"""The capability executor (T206): the missing link from a catalog declaration to a live Lua call.

The integration-readiness audit (specs/002-civ-playing-harness/integration-readiness.md) found the
single most serious gap in the whole harness: ``IntegrationCapability.implementation_ref`` -- the
field binding a catalog declaration to its ``lua/`` file -- was read nowhere outside its own model
definition. 21 declared Lua files and 51 catalog declarations existed; nothing ever loaded or
executed any of them. This module is that missing link. :class:`CapabilityExecutor` is the single
choke point every observation and action dispatch (T207, T208) goes through: resolve a declaration
via :class:`~civsim_harness.capability.registry.CapabilityRegistry`, refuse it outright if the
caller's own context does not match what the declaration was declared for (``registry.authorize``
-- see that module's own docstring, which names this module as "the dispatcher" it was built to
gate), load the capability's ``implementation_ref`` Lua file from disk, resolve which function in
that file's own dispatch table answers this declaration, and send the whole thing through
``NexusClient.execute_command`` -- never fabricating or substituting a result on any failure.

**Selecting the Lua function for a declaration_id.** ``catalogs/README.md`` §3 documents that every
``lua/**/*.lua`` file "defines one function per served declaration ... the dispatcher ... is
responsible for selecting the function for the requested declaration_id" -- but no catalog field
names that function; it is a convention baked into how the Lua files were authored. Verified by
mechanically comparing every one of the real catalog's 51 declarations against every one of the 21
real ``lua/**/*.lua`` files' own final ``CivSim_X = { key = fn, ... }`` dispatch table: the
overwhelming majority (44 of 51 -- every ``*.state`` observation and every single-function or
same-domain multi-function action file, e.g. ``units.move_to``/``units.found_city``/
``units.promote`` all keying directly off ``lua/ingame/unit_orders.lua``'s
``move_to``/``found_city``/``promote``) resolve by one fixed rule: **the declaration_id's own
last dot-segment is the table key.**
:data:`_FUNCTION_KEY_OVERRIDES` and the ``prompts.orders`` rule below are the complete, exhaustive
set of exceptions to that rule in the real catalog, each with its own reason recorded next to it --
not a guess, and not partial coverage silently left as a gap.

**Views are out of scope for this module's caller (T207).** ``kind: view`` declarations resolve
camera state and screening through ``observe/capture.py``'s own, separate camera-positioning path,
not through a fresh call to this executor per decision step -- nothing here special-cases ``view``,
but nothing calls it for one either.

**State indices are resolved by name, fresh, on every call -- never cached.** A live capture
(``nexus/client.py``'s own module docstring, 2026-09-20) established that Lua state indices differ
by game *phase*, and a stale index is frequently still valid in the new table for an unrelated
state, so reusing one raises nothing at all -- the wrong Lua just runs. :func:`_resolve_state_index`
follows the exact pattern ``saves/save_game.py`` was reworked to use for the same reason: it takes a
live session object (typically the connected ``NexusClient`` itself) and reads
``session.state_indices.by_name[context.value]`` fresh on every :meth:`CapabilityExecutor.execute`
call, never captured once at construction and never memoised anywhere in this module.

**Lua source is cached by path -- state indices never are.** The Lua files are immutable within one
catalog version (the catalog's own content hash already assumes this), so re-reading a file's bytes
and re-parsing its dispatch table on every call would be pure overhead; :class:`CapabilityExecutor`
caches ``(source, module_name, table_keys)`` keyed by ``implementation_ref`` the first time each
capability is used. Nothing about that cache is ever consulted for a state index.

**No shared Lua helper is ever injected.** Every ``lua/**/*.lua`` file is self-contained by design
(the sandbox has no ``require``, ``io``, ``debug``, or JSON library -- each file carries its own
hand-rolled ``CivSim_JsonEncode``); this module sends each file's own source verbatim, with one
appended call-and-print line, and never concatenates two files together or factors a helper out.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import CatalogError, NexusError
from civsim_harness.models.common import CapabilityId, DeclarationId, LuaContext
from civsim_harness.observe.assemble import CapabilityResult

# --------------------------------------------------------------------------
# State index resolution -- by name, fresh per call, never cached (see module docstring;
# mirrors saves/save_game.py's InGameStateIndexSource pattern, generalised to both contexts).
# --------------------------------------------------------------------------


class _StateIndicesLike(Protocol):
    """Structural shape of ``civsim_harness.nexus.client.StateIndices`` -- duck-typed, never
    imported, so this module keeps no hard dependency on the transport."""

    @property
    def by_name(self) -> Mapping[str, int]: ...


@runtime_checkable
class _HasStateIndices(Protocol):
    """Structural shape of a connected ``NexusClient`` (or anything alike): "the client itself",
    read fresh on every call rather than snapshotted once."""

    @property
    def state_indices(self) -> _StateIndicesLike | None: ...


def _resolve_state_index(session: _HasStateIndices, context: LuaContext) -> int:
    """Ask *session* for *context*'s current Lua state index, by name, right now.

    Never a value captured earlier -- see the module docstring on why a cached index can silently
    target the wrong Lua state after a phase transition or reconnect.
    """
    indices = session.state_indices
    if indices is None:
        raise NexusError(
            "Cannot resolve a Lua state index -- this Nexus session has not connected, or no "
            "LSQ: query has completed yet, so no state table is available to dispatch against",
            detail={"reason": "state_indices_unresolved", "context": str(context)},
        )
    index = indices.by_name.get(context.value)
    if index is None:
        raise NexusError(
            f"Required Lua state {context.value!r} is not present in the current state table -- "
            "it is either not yet available (no game loaded) or was resolved before a phase "
            "transition invalidated it; call refresh_state_indices() or resolve_game_states() "
            "again rather than reusing a previously resolved index",
            detail={
                "reason": "context_state_unavailable",
                "context": str(context),
                "known_states": sorted(indices.by_name),
            },
        )
    return index


# --------------------------------------------------------------------------
# Lua dispatch-table parsing (cached by path -- see module docstring)
# --------------------------------------------------------------------------

# Matches one top-level "CivSim_X = { ... }" assignment (module.README.md §3's convention: every
# lua/**/*.lua file ends with exactly one such table). Deliberately does not match a `local`
# declaration (those are the file's own private helpers, e.g. CivSim_JsonEncode, never dispatch
# entry points).
_DISPATCH_TABLE_RE = re.compile(r"^(CivSim_\w+)\s*=\s*\{(.*?)\n\}", re.MULTILINE | re.DOTALL)
# Within that table's body, one entry per served declaration: `key = CivSim_SomeFunctionName`.
_DISPATCH_KEY_RE = re.compile(r"(\w+)\s*=\s*CivSim_\w+")


def _parse_dispatch_table(source: str, *, implementation_ref: str) -> tuple[str, frozenset[str]]:
    """Extract the one dispatch table a Lua file declares: ``(module_name, {exposed keys})``."""
    matches = list(_DISPATCH_TABLE_RE.finditer(source))
    if len(matches) != 1:
        raise CatalogError(
            "Lua file does not declare exactly one top-level CivSim_* dispatch table "
            "(catalogs/README.md §3's authoring convention)",
            detail={"implementation_ref": implementation_ref, "tables_found": len(matches)},
        )
    module_name, body = matches[0].groups()
    keys = frozenset(_DISPATCH_KEY_RE.findall(body))
    if not keys:
        raise CatalogError(
            "Lua dispatch table declares no callable entries",
            detail={"implementation_ref": implementation_ref, "module_name": module_name},
        )
    return module_name, keys


# --------------------------------------------------------------------------
# declaration_id -> Lua dispatch-table function key
# --------------------------------------------------------------------------

# The complete, exhaustive set of declarations in the real catalog whose Lua function key is not
# their own declaration_id's last dot-segment (see module docstring for the verification method).
_FUNCTION_KEY_OVERRIDES: Mapping[DeclarationId, str] = {
    # lua/ingame/turn_control.lua's CivSim_TurnControl exposes {end_turn, read_turn_number}; this
    # observation's own last segment ("turn_state") names the *domain* it reports on, not the
    # accessor -- turn.end_turn (the action sharing this same capability_id) does match directly.
    DeclarationId("game.turn_state"): "read_turn_number",
    # lua/ingame/screens.lua's CivSim_Screens table is shared by two distinct capability_ids
    # (screens.probe here; prompts.orders below) -- "screen_state" is not a key in that table at
    # all; the read-only probe function is.
    DeclarationId("game.screen_state"): "probe",
    # lua/ingame/empire_orders.lua names this accessor after what it sets (a *tech*:
    # set_research), not after this declaration's own domain prefix ("research").
    DeclarationId("research.set_tech"): "set_research",
}

# catalogs/actions/prompts.yaml's own header: every prompts.* declaration_id (nine of them) shares
# one generic function, lua/ingame/screens.lua's CivSim_Screens.respond(promptType, optionId) --
# not one function per declaration like every other multi-declaration capability in this catalog.
# A capability_id rule, not nine individual overrides, since it is the capability itself that is
# generic here, not each declaration.
_PROMPT_CAPABILITY_ID = CapabilityId("prompts.orders")
_PROMPT_FUNCTION_KEY = "respond"


def _resolve_function_key(
    *, declaration_id: DeclarationId, capability_id: CapabilityId, table_keys: frozenset[str]
) -> str:
    """Resolve which key in a Lua file's own dispatch table answers *declaration_id*.

    Raises :class:`~civsim_harness.errors.CatalogError` -- never guesses, never falls back to an
    empty or fabricated call -- when no rule (the explicit override table, the ``prompts.orders``
    capability rule, or the default last-dot-segment convention) names a key actually present in
    the file's own table.
    """
    override = _FUNCTION_KEY_OVERRIDES.get(declaration_id)
    candidate = override if override is not None else None
    if candidate is None and capability_id == _PROMPT_CAPABILITY_ID:
        candidate = _PROMPT_FUNCTION_KEY
    if candidate is None:
        candidate = str(declaration_id).rsplit(".", 1)[-1]

    if candidate not in table_keys:
        raise CatalogError(
            "no Lua dispatch function could be resolved for this declaration_id",
            detail={
                "declaration_id": str(declaration_id),
                "capability_id": str(capability_id),
                "attempted_key": candidate,
                "available_functions": sorted(table_keys),
            },
        )
    return candidate


# --------------------------------------------------------------------------
# Python value -> Lua literal (for the appended call's arguments)
# --------------------------------------------------------------------------


def _lua_literal(value: Any) -> str:
    """Encode *value* as a Lua literal expression, for splicing into a dispatched call's argument
    list. The inverse counterpart of every lua/**/*.lua file's own hand-rolled
    ``CivSim_JsonEncode`` -- Python data going *into* a call rather than a Lua table's JSON coming
    back out."""
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    if isinstance(value, Mapping):
        parts = ", ".join(f"[{_lua_literal(str(k))}]={_lua_literal(v)}" for k, v in value.items())
        return "{" + parts + "}"
    if isinstance(value, (list, tuple)):
        parts = ", ".join(_lua_literal(item) for item in value)
        return "{" + parts + "}"
    raise TypeError(f"cannot encode {type(value).__name__!r} as a Lua literal argument")


# --------------------------------------------------------------------------
# CapabilityExecutor
# --------------------------------------------------------------------------


class CapabilityExecutor:
    """Resolve, load, and dispatch one catalog declaration's capability (T206).

    The one class every production :class:`~civsim_harness.observe.reader.ObservationReader`
    (T207) and :class:`~civsim_harness.act.executor.ActionExecutor` (T208) call to actually reach
    the game -- everything upstream of this class (the registry, the predicate evaluator, the
    assembler) is pure and Nexus-free by design; this is where that stops.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        execute_command: Callable[[int, str], Awaitable[Any]],
        session: _HasStateIndices,
        lua_root: Path | str = Path("."),
    ) -> None:
        """*execute_command* is typically a thin closure over
        :meth:`~civsim_harness.nexus.client.NexusClient.execute_command`, called positionally as
        ``execute_command(state_index, lua_body)`` -- the same seam shape
        ``saves.save_game.LuaSaveCapability`` already uses, so this class has no hard dependency
        on the transport for its own logic. *session* is typically the connected ``NexusClient``
        itself (its ``.state_indices`` is read fresh on every :meth:`execute` call -- see the
        module docstring; never snapshotted here). *lua_root* is the directory
        ``IntegrationCapability.implementation_ref`` paths (e.g. ``lua/ingame/turn_control.lua``)
        are resolved relative to -- the repository root, not ``catalogs/``.
        """
        if not isinstance(session, _HasStateIndices):
            raise TypeError(
                "session must expose a `.state_indices` property (e.g. a connected NexusClient) "
                "-- never a bare state index or resolver captured once, which can silently go "
                "stale after a reconnect or phase transition (see this module's docstring); got "
                f"{type(session).__name__!r}"
            )
        self._registry = registry
        self._execute_command = execute_command
        self._session = session
        self._lua_root = Path(lua_root)
        self._dispatch_table_cache: dict[str, tuple[str, str, frozenset[str]]] = {}

    async def execute(
        self,
        declaration_id: DeclarationId,
        *,
        context: LuaContext,
        arguments: Sequence[Any] = (),
    ) -> CapabilityResult:
        """Resolve *declaration_id*, load its capability's Lua file, dispatch it in *context*, and
        return a :class:`~civsim_harness.observe.assemble.CapabilityResult`.

        *context* is the caller's own declared intent for which Lua context this dispatch belongs
        in -- checked against the declaration's own ``context`` via
        :meth:`~civsim_harness.capability.registry.CapabilityRegistry.authorize`, which raises
        :class:`~civsim_harness.capability.registry.WrongContextError` on a mismatch (FR-022,
        research R3). This is the only gate standing between an observation dispatch and a
        ``GameCore_Tuner`` context where ``UI``/``Network``/``UIManager`` are entirely absent (a
        live spike confirmed every such call errors at the root there) -- action dispatch already
        has its own upstream gate in ``act/dispatch.py``, but observation dispatch (T207) has none
        but this one.

        Raises :class:`~civsim_harness.errors.CatalogError` for an unresolvable declaration, a
        missing ``implementation_ref`` file, or a Lua dispatch function that cannot be resolved,
        and :class:`~civsim_harness.errors.NexusError` for an unresolved state index or any
        transport/Lua-side failure -- never returns a fabricated or empty result on any of these.
        """
        declaration = self._registry.authorize(declaration_id, context)
        capability = self._registry.capability_for(declaration_id)

        source, module_name, table_keys = self._load_dispatch_table(capability.implementation_ref)
        function_key = _resolve_function_key(
            declaration_id=declaration_id,
            capability_id=capability.capability_id,
            table_keys=table_keys,
        )

        state_index = _resolve_state_index(self._session, declaration.context)
        args_source = ", ".join(_lua_literal(argument) for argument in arguments)
        lua_body = (
            f"{source}\n\n"
            f"print(CivSim_JsonEncode({module_name}.{function_key}({args_source})))"
        )

        value = await self._execute_command(state_index, lua_body)
        return CapabilityResult(declaration_id=declaration_id, value=value)

    def _load_dispatch_table(self, implementation_ref: str) -> tuple[str, str, frozenset[str]]:
        """Load and parse *implementation_ref*'s Lua source, cached by path (never a state index --
        see the module docstring). Raises :class:`~civsim_harness.errors.CatalogError` when the
        file does not exist, rather than returning an empty/fabricated dispatch table."""
        cached = self._dispatch_table_cache.get(implementation_ref)
        if cached is not None:
            return cached

        path = self._lua_root / implementation_ref
        if not path.is_file():
            raise CatalogError(
                "implementation_ref does not resolve to a file on disk",
                detail={"implementation_ref": implementation_ref, "resolved_path": str(path)},
            )
        source = path.read_text(encoding="utf-8")
        module_name, table_keys = _parse_dispatch_table(
            source, implementation_ref=implementation_ref
        )

        result = (source, module_name, table_keys)
        self._dispatch_table_cache[implementation_ref] = result
        return result
