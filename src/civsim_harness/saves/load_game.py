"""Save loading (T217, FR-004, FR-033, FR-045/FR-046) -- **FireTuner path, verified against a
real Civilization VI client.**

The T217 spike (``specs/002-civ-playing-harness/spikes/t217-RESOLVED-frontend-loadgame.md``)
established, end to end and measured, that ``Network.LoadGame`` works from Lua -- the earlier
finding that it "does not work from Lua" (``spikes/load-path-linux.md``) is retracted by that
spike. Everything this module does is a direct transcription of what that spike measured:

- **The call is front-end only.** ``Network.LoadGame`` exists and is callable from ``InGame``, but
  returns ``false`` there -- which reads as "bad argument shape" and actually means "not from
  here". Firaxis's own shipped automation guards its load test with ``UI.IsInFrontEnd()`` and
  issues ``Events.ExitToMainMenu()`` first (``automation_dailysmoketest.lua:241``); this loader
  does exactly the same, which is also the parity path -- exiting to the menu and picking a save
  in the Load Game menu is precisely what a human does (Principle I).
- **The working call shape** is a table -- ``Location=SaveLocations.LOCAL_STORAGE``,
  ``Type=SaveTypes.SINGLE_PLAYER``, ``Directory=SaveDirectories.DEFAULT``, ``Name=<bare save
  name, no extension, no path>`` -- passed with ``ServerType.SERVER_TYPE_NONE``, issued from a
  front-end state that has both the function and the enums. ``MainMenu`` is the verified state;
  the enums are absent from ``Main State``, so the call must not be issued there.
- **The tuner port closes during the load** and rebinds when the game is up (measured: refused
  for the whole load, rebound within ~5s of the game becoming interactive). A connection refused
  or dropped mid-load is therefore an *expected* phase of a successful load, never treated as a
  crash here.
- **Verification is a far-side assertion on a fresh connection after the phase transition** --
  never the call's own return value (the standing rule: for anything crossing a process or
  protocol boundary, assert on what the far side received, never on what our side returned).
  After the game states reappear, this loader reads ``Game.GetCurrentGameTurn()``,
  ``Game.GetLocalPlayer()`` and ``UI.IsInFrontEnd()`` back from ``InGame`` and compares the turn
  against what the :class:`~civsim_harness.models.records.SavePoint` recorded. A load that lands
  anywhere but the named save's exact position **fails the operation** (Principle IV), never
  continues silently.
- **One honestly-open item, carried from the spike:** a load may stop on the leader-intro screen
  (``... JOINS THE WORLD STAGE`` / ``CONTINUE GAME``) with the tuner port still closed, possibly
  only for saves taken before that screen was first dismissed. This loader cannot click it; the
  in-game wait's timeout message names the possibility so a live operator knows what to look at.

**State indices are re-resolved at every phase boundary this module crosses** -- exiting to the
menu, and the load itself -- via ``refresh_state_indices()``/``reconnect()`` on the same
:class:`~civsim_harness.nexus.client.NexusClient` the run dispatches everything else through
(the game accepts one tuner connection at a time, research R4, so the loader must drive that one
connection rather than opening a second). Indices are never cached across a transition
(``nexus/client.py``'s module docstring: a stale index is frequently still *valid* for an
unrelated state, so reusing one raises nothing at all).

**What stays live-unverified.** The Lua bodies below follow the verified call shapes exactly and
print their JSON results through the shared sentinel helpers (the tuner has no return channel),
but this module itself has run only against the recorded-transcript fake -- a live session must
still confirm the end-to-end sequence (see the T217 task note in
``specs/002-civ-playing-harness/tasks.md``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeGuard

from civsim_harness.errors import HarnessError, NexusError, PreflightError
from civsim_harness.host.port import (
    GameProcess,
    HostPlatform,
    InputEvent,
    InputEventKind,
    InputStatus,
)
from civsim_harness.models.records import SavePoint
from civsim_harness.nexus.client import NexusClient, StateIndices
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json
from civsim_harness.saves.verify import SAVE_FILE_SUFFIX, resolve_saves_dir

#: The two Lua state names this loader steers between. ``InGame`` exists only while a game is
#: loaded; ``MainMenu`` is the front-end state the spike verified carries both ``Network.LoadGame``
#: and the enums the call table needs.
#: The key that dismisses Civilization VI's leader-intro screen after a load.
#: MEASURED, not chosen (spikes/r7-live-client-session-linux.md): `Escape` works;
#: `Return` and `space` do NOT. The screen appears on EVERY load -- not only on
#: saves taken before it was first dismissed -- and holds the tuner port closed
#: indefinitely (150s observed with no recovery), so without this the load path
#: can only ever time out.
INTRO_DISMISS_KEY = "Escape"

IN_GAME_STATE_NAME = "InGame"
MAIN_MENU_STATE_NAME = "MainMenu"

#: Bound on reaching the front end (``MainMenu`` present, ``InGame`` gone) after
#: ``Events.ExitToMainMenu()`` -- and on the initial phase read, which shares the same wait
#: machinery. The exit transition is seconds on a live client; 90s is deliberately generous
#: because an honest timeout naming what never happened beats a premature one.
DEFAULT_EXIT_TO_MENU_TIMEOUT_S = 90.0

#: Bound on the load itself: from ``Network.LoadGame`` returning ``true`` to the game states
#: reappearing on a rebound tuner connection. Measured: the port is refused for the whole load
#: and rebinds within ~5s of the game becoming interactive -- but the leader-intro screen can
#: hold the port closed indefinitely (spike, "One open item"), so this bound is generous and its
#: timeout message names that screen explicitly.
DEFAULT_LOAD_TIMEOUT_S = 300.0

#: Bound on the far-side read-back after the game states have reappeared. Transient
#: unreadability (a connection dropped in the rebind window, a ``pcall`` error while the game
#: finishes coming up) is retried inside this bound; a *mismatched position* is never retried --
#: it fails immediately (Principle IV).
DEFAULT_VERIFY_TIMEOUT_S = 30.0

#: How long to wait between polls of the client's state table while a transition is in flight.
DEFAULT_POLL_INTERVAL_S = 1.0


def _require_clean_save_name(save_name: str) -> None:
    """*save_name* is spliced into a double-quoted Lua string literal below. Save names are
    harness-generated (``saves/save_point.py``'s ``civsim__<run_id>__t<turn:04d>``) and the
    `SavePoint` model enforces that overall shape, but its ``.+`` run-id segment does not itself
    forbid a quote or backslash -- so one is rejected here outright rather than escaped, exactly
    as ``nexus/sentinels.py``'s ``_json_key`` treats its own identifiers: a quote in a save name
    would be a bug in the caller, never input to accommodate."""
    if any(ch in save_name for ch in ('"', "\\", "\n", "\r")):
        raise HarnessError(
            "save name contains characters that cannot be spliced into the Lua load call "
            "(quote, backslash, or newline); refusing rather than escaping a name no "
            "harness-generated save can legitimately carry",
            detail={"save_name": save_name},
        )


def _build_exit_to_menu_lua() -> str:
    """``Events.ExitToMainMenu()`` from ``InGame`` -- the exact precondition step Firaxis's own
    shipped load test performs (``automation_dailysmoketest.lua:241``). ``pcall``-wrapped and
    **printed** as JSON, never ``return``-ed: the tuner reads only printed output between the
    nonce sentinels (``nexus/sentinels.py``), so a body ending in ``return`` yields nothing."""
    return (
        LUA_JSON_PRELUDE
        + "local ok, err = pcall(function() Events.ExitToMainMenu() end); "
        + lua_print_json({"issued": "ok", "error": '(ok and "") or tostring(err)'})
    )


def _build_load_lua(save_name: str) -> str:
    """The verified front-end load call (spike, "The working recipe"), assembled by concatenation
    for the same reason ``saves/save_game.py``'s ``_build_save_lua`` is: the JSON prelude is full
    of Lua braces a ``str.format`` template would need escaped throughout.

    ``pcall(function() return Network.LoadGame(...) end)`` yields ``ok`` (the pcall did not
    error) and ``ret`` (the call's own return on success, or the error object on failure), so:

    - ``issued``   -> ``ok``: the call itself ran without a Lua error;
    - ``accepted`` -> ``(ok and ret) == true``: the client accepted the load. ``false`` is how
      the client refuses -- from the wrong phase, or for a save it cannot load;
    - ``error``    -> the pcall error text when ``ok`` is false, else ``""``.
    """
    _require_clean_save_name(save_name)
    return (
        LUA_JSON_PRELUDE
        + "local loadGame = {}; "
        + f'loadGame.Name = "{save_name}"; '
        + "loadGame.Location = SaveLocations.LOCAL_STORAGE; "
        + "loadGame.Type = SaveTypes.SINGLE_PLAYER; "
        + "loadGame.IsAutosave = false; "
        + "loadGame.IsQuicksave = false; "
        + "loadGame.Directory = SaveDirectories.DEFAULT; "
        + "local ok, ret = pcall(function() "
        + "return Network.LoadGame(loadGame, ServerType.SERVER_TYPE_NONE) end); "
        + lua_print_json(
            {
                "issued": "ok",
                "accepted": "(ok and ret) == true",
                "error": '(ok and "") or tostring(ret)',
            }
        )
    )


def _build_verify_lua() -> str:
    """The far-side position read-back, run in ``InGame`` after the load's phase transition --
    the same three facts the spike's own far-side assertion read on a fresh connection. Fields
    the ``pcall`` never reached print as ``null`` and fail the checks in Python; nothing here
    fabricates a value for a fact the client did not report."""
    return (
        LUA_JSON_PRELUDE
        + "local turn, player, front_end; "
        + "local ok, err = pcall(function() "
        + "turn = Game.GetCurrentGameTurn(); "
        + "player = Game.GetLocalPlayer(); "
        + "front_end = UI.IsInFrontEnd() "
        + "end); "
        + lua_print_json(
            {
                "issued": "ok",
                "error": '(ok and "") or tostring(err)',
                "turn": "turn",
                "local_player": "player",
                "in_front_end": "front_end",
            }
        )
    )


def _is_plain_number(value: Any) -> TypeGuard[int | float]:
    """A JSON number that is genuinely a number -- ``bool`` is an ``int`` subclass in Python, and
    a Lua ``true`` leaking into a numeric field must not compare equal to ``1``."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_front_end(indices: StateIndices) -> bool:
    """The front-end phase: ``MainMenu`` present and ``InGame`` gone. Both halves matter -- the
    in-game state table carries many menu-named UI states, so ``MainMenu`` alone is not proof the
    exit transition has completed."""
    return (
        MAIN_MENU_STATE_NAME in indices.by_name and IN_GAME_STATE_NAME not in indices.by_name
    )


class LuaSaveLoader:
    """Production ``SaveLoader`` (T217): load a named save back into the running client through
    the verified front-end ``Network.LoadGame`` path.

    Satisfies both structural ``SaveLoader`` protocols in this codebase
    (``resilience/recovery.py``'s and ``saves/branching.py``'s -- the same
    ``async def load(save: SavePoint) -> None`` shape), so one implementation serves mid-turn
    recovery (FR-045/FR-046), operator ``resume-from`` rewinds (FR-004), and branch creation
    (FR-033).

    **Principle I parity basis.** Everything here is what a human does to load a save: exit to
    the main menu, pick the named save in the Load Game menu. Nothing read during the load or its
    verification reaches the agent's context -- the read-back exists solely to fail the operation
    when the client is not at the named save's exact position (Principle IV).

    **Failure discipline.** Every step fails loudly and specifically, naming what was expected
    and what was seen. The one exception-shape with special meaning: ``FileNotFoundError`` when
    the save's ``.Civ6Save`` is absent from the resolved save directory (checked before the
    client is touched, when a *host* was supplied) -- that is the signal
    ``RecoveryEngine.recover`` uses to record ``save_missing`` durably and fail the run outright
    rather than retrying a load that can only find the same file gone (T172, FR-036).

    *client* is the run's own connected :class:`~civsim_harness.nexus.client.NexusClient` -- the
    tuner accepts one connection at a time, so the loader drives (and, across the load's own
    port-closure, reconnects) that same instance; the fresh indices it resolves on the way are
    therefore the ones every later per-turn call on that client sees.
    """

    def __init__(
        self,
        client: NexusClient,
        *,
        host: HostPlatform | None = None,
        home: Path | None = None,
        exit_to_menu_timeout_s: float = DEFAULT_EXIT_TO_MENU_TIMEOUT_S,
        load_timeout_s: float = DEFAULT_LOAD_TIMEOUT_S,
        verify_timeout_s: float = DEFAULT_VERIFY_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        """*host*/*home* enable the pre-load filesystem existence check (the same
        ``resolve_saves_dir`` path ``saves/verify.py`` confirms every quicksave through); pass
        ``host=None`` to skip it on a caller that genuinely has no host port. The timeouts are
        injectable so tests do not wait wall-clock minutes for an honest failure."""
        self._client = client
        self._host = host
        self._home = home
        self._exit_to_menu_timeout_s = exit_to_menu_timeout_s
        self._load_timeout_s = load_timeout_s
        self._verify_timeout_s = verify_timeout_s
        self._poll_interval_s = poll_interval_s
        self._intro_presses = 0
        self._intro_skipped: str | None = None

    async def load(self, save: SavePoint) -> None:
        """Load *save* into the game client, replacing its current state.

        The sequence, each step measured by the T217 spike (module docstring):

        1. Refuse a save whose ``.Civ6Save`` is already gone (``FileNotFoundError`` -- the
           recovery engine's own "genuinely missing" signal), before the client is touched.
        2. Re-resolve the client's phase fresh (never a cached table). From ``InGame``, issue
           ``Events.ExitToMainMenu()`` and wait -- bounded -- for the front end.
        3. Issue the verified ``Network.LoadGame`` table from ``MainMenu``. A delivered ``false``
           fails immediately; a response lost to the load's own port-closure does not (the far
           side decides, step 4).
        4. Wait -- bounded, treating refused connections as the expected mid-load state -- for
           the game states to reappear on a fresh connection, then assert the far side: not in
           the front end, a local player resolved, and ``Game.GetCurrentGameTurn()`` equal to the
           turn *save* records. Any mismatch fails the operation (Principle IV).
        """
        # Checked before anything is touched: a save name the Lua call cannot carry must not
        # first kick a live client out of its game only to fail at the load call.
        _require_clean_save_name(save.save_name)
        self._require_present_on_disk(save)

        # -- 2. where is the client right now? Resolved fresh, at this known phase boundary. ----
        indices = await self._await_phase(
            predicate=lambda st: IN_GAME_STATE_NAME in st.by_name or _is_front_end(st),
            timeout_s=self._exit_to_menu_timeout_s,
            waiting_for=(
                "a reachable tuner reporting a recognisable phase (InGame, or the front end "
                "with MainMenu present)"
            ),
            save=save,
        )

        if IN_GAME_STATE_NAME in indices.by_name:
            await self._exit_to_main_menu(indices.by_name[IN_GAME_STATE_NAME], save)
            indices = await self._await_phase(
                predicate=_is_front_end,
                timeout_s=self._exit_to_menu_timeout_s,
                waiting_for=(
                    "the front end (MainMenu present, InGame gone) after "
                    "Events.ExitToMainMenu()"
                ),
                save=save,
            )

        self._intro_presses = 0
        self._intro_skipped = None

        # -- 3. the verified front-end load call ------------------------------------------------
        lost_response = await self._issue_load(indices.by_name[MAIN_MENU_STATE_NAME], save)

        # -- 4a. the phase transition: port closed during the load is the expected shape --------
        hint = (
            "the tuner port closes for the whole load and rebinds ~5s after the game becomes "
            "interactive; a load stopped on the leader-intro screen (CONTINUE GAME) holds the "
            "port closed indefinitely and needs one manual dismissal click "
            "(spikes/t217-RESOLVED-frontend-loadgame.md, 'One open item')"
        )
        if lost_response is not None:
            hint += (
                f"; note the Network.LoadGame call's own response was never delivered "
                f"({lost_response}), so whether the client accepted the load was never reported"
            )
        await self._await_phase(
            predicate=lambda st: st.has_game_states,
            timeout_s=self._load_timeout_s,
            waiting_for=(
                "the game states (GameCore_Tuner, InGame) to reappear after Network.LoadGame "
                f"accepted save '{save.save_name}'"
            ),
            save=save,
            hint=hint,
            on_unreachable=self._dismiss_intro_screen,
        )

        # -- 4b. the far-side assertion ---------------------------------------------------------
        await self._verify_far_side(save)

    # -- steps ----------------------------------------------------------------------------------

    def _require_present_on_disk(self, save: SavePoint) -> None:
        """T172/FR-036: a save whose file is gone is refused before the client is touched, as
        ``FileNotFoundError`` -- the one exception shape ``RecoveryEngine.recover`` maps to a
        durable ``save_missing`` record and an outright, never-retried failure. Resolved through
        the same ``HostPlatform`` port ``saves/verify.py`` confirms every quicksave through --
        never a hard-coded path. Skipped only when no *host* was supplied at construction."""
        if self._host is None:
            return
        path = resolve_saves_dir(self._host, home=self._home) / (
            f"{save.save_name}{SAVE_FILE_SUFFIX}"
        )
        if not path.is_file():
            raise FileNotFoundError(
                f"save '{save.save_name}' (save_point_id={save.save_point_id}) has no "
                f".Civ6Save at {path}; the file this SavePoint records is gone from the "
                "resolved save directory, so the load is refused before touching the client "
                "(FR-036: reported, never retried, never retargeted)"
            )

    async def _exit_to_main_menu(self, in_game_index: int, save: SavePoint) -> None:
        """Issue ``Events.ExitToMainMenu()`` in ``InGame``. A *delivered* Lua error fails
        immediately; a response lost in flight does not -- the phase transition this very call
        starts can tear down the InGame state (or the connection) before the printed result gets
        out, and the bounded front-end wait that follows is the real assertion (the far-side
        rule)."""
        try:
            result = await self._client.execute_command(
                state_index=in_game_index, lua_body=_build_exit_to_menu_lua()
            )
        except NexusError:
            return
        if not isinstance(result, dict) or not result.get("issued"):
            lua_error = result.get("error") if isinstance(result, dict) else None
            raise HarnessError(
                "Events.ExitToMainMenu() reported a Lua-side error, so the client cannot be "
                f"brought to the front end to load save '{save.save_name}': expected the exit "
                f"call to issue without error, saw {result!r}",
                detail={
                    "save_name": save.save_name,
                    "save_point_id": save.save_point_id,
                    "result": result,
                    "lua_error": lua_error,
                },
            )

    async def _issue_load(self, main_menu_index: int, save: SavePoint) -> str | None:
        """Issue the verified ``Network.LoadGame`` table from ``MainMenu``.

        Returns ``None`` when the client reported ``true``, or -- when the response was lost to
        the load's own port-closure before delivery -- a short description of that transport
        failure, for the in-game wait's timeout message to carry. A *delivered* refusal
        (``false``) or Lua error raises immediately: those are the client answering "no", not
        the transition eating the answer."""
        try:
            result = await self._client.execute_command(
                state_index=main_menu_index, lua_body=_build_load_lua(save.save_name)
            )
        except NexusError as exc:
            return f"{type(exc).__name__}: {exc.message}"
        if not isinstance(result, dict) or not result.get("issued"):
            lua_error = result.get("error") if isinstance(result, dict) else None
            raise HarnessError(
                f"the Network.LoadGame call for save '{save.save_name}' errored in Lua before "
                f"the client could consider it: expected an issued call, saw {result!r}",
                detail={
                    "save_name": save.save_name,
                    "save_point_id": save.save_point_id,
                    "result": result,
                    "lua_error": lua_error,
                },
            )
        if result.get("accepted") is not True:
            raise HarnessError(
                f"Network.LoadGame returned false for save '{save.save_name}' issued from the "
                "MainMenu state -- expected true, the verified front-end acceptance "
                "(spikes/t217-RESOLVED-frontend-loadgame.md). false is how the client refuses "
                "the call: either this state cannot issue loads after all, or the named save "
                "is not loadable from Location=LOCAL_STORAGE Type=SINGLE_PLAYER "
                "Directory=DEFAULT under exactly this name",
                detail={
                    "save_name": save.save_name,
                    "save_point_id": save.save_point_id,
                    "result": result,
                },
            )
        return None

    def _dismiss_intro_screen(self) -> None:
        """Press `Escape` once, aimed at the game, while the tuner port is closed.

        Called from `_await_phase`'s unreachable branches during step 4a only -- so it
        runs exactly while the port is down, and stops the moment the port answers. It
        therefore cannot leave a stray keystroke in the running game.

        **Why a retry and not one timed press.** The port closes the instant the load is
        issued, but the intro screen only appears when the load *finishes*, tens of seconds
        later -- and the screen cannot be observed through the tuner, because the tuner is
        what it is holding closed. The only observable is the port coming back, which
        happens *after* dismissal succeeds. A single press at a fixed delay was tried and
        measured failing; the retry shape is what passed (40.1s vs a 160.4s timeout).

        **Focus first, always.** Synthetic input has no window targeting, so a press
        without focus goes to whatever the operator has in front of them. A non-`ok`
        `focus_window` means we do not press at all -- the reason is kept for the timeout
        error, so a run that failed this way says why instead of looking like a dead client.
        """
        if self._host is None:
            self._intro_skipped = "no host port was supplied, so no key could be sent"
            return
        try:
            process = self._host.locate_game_process()
            if process is None:
                self._intro_skipped = "the game process could not be located"
                return
            window = self._host.find_game_window(process)
            if window is None:
                self._intro_skipped = "the game window could not be resolved"
                return
            focused = self._host.focus_window(window)
            if focused.status is not InputStatus.ok:
                # Never press at an unfocused window: the keystroke would land
                # wherever the operator is looking. Refusing loudly is the point.
                self._intro_skipped = (
                    f"focus_window reported {focused.status.value} "
                    f"({focused.reason}), so no key was sent"
                )
                return
            sent = self._host.send_input(
                [InputEvent(kind=InputEventKind.key_press, key=INTRO_DISMISS_KEY)]
            )
            if sent.status is not InputStatus.ok:
                self._intro_skipped = (
                    f"send_input reported {sent.status.value} ({sent.reason})"
                )
                return
            self._intro_presses += 1
            self._intro_skipped = None
        except Exception as exc:  # noqa: BLE001
            # A failed keystroke must never break the wait it is helping: the
            # deadline in `_await_phase` stays the single authority on giving up.
            self._intro_skipped = f"{type(exc).__name__}: {exc}"

    async def _await_phase(
        self,
        *,
        predicate: Any,
        timeout_s: float,
        waiting_for: str,
        save: SavePoint,
        hint: str | None = None,
        on_unreachable: Callable[[], None] | None = None,
    ) -> StateIndices:
        """Poll the client's state table -- bounded -- until *predicate* holds, reconnecting as
        needed. A refused connection or dropped socket is an *expected* observation mid-load
        (the spike measured the tuner port closed for the whole load), so both are recorded and
        retried rather than raised; what is never silent is the deadline, whose error names what
        was awaited, for how long, the last state table seen, and the last transport failure."""
        client = self._client
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        needs_reconnect = not client.is_connected
        last_error: str | None = None
        last_states: list[str] | None = None
        while True:
            try:
                if needs_reconnect:
                    indices = await client.reconnect()
                    needs_reconnect = False
                else:
                    indices = await client.refresh_state_indices()
            except (NexusError, PreflightError) as exc:
                last_error = f"{type(exc).__name__}: {exc.message}"
                needs_reconnect = True
                if on_unreachable is not None:
                    on_unreachable()
            except OSError as exc:
                # A raw socket error (Windows surfaces a peer-closed connection as
                # ConnectionResetError on the next write, rather than a clean EOF the client
                # would wrap as NexusError) -- the same expected mid-transition shape.
                last_error = f"{type(exc).__name__}: {exc}"
                needs_reconnect = True
                if on_unreachable is not None:
                    on_unreachable()
            else:
                last_states = sorted(indices.by_name)
                if predicate(indices):
                    return indices
            if loop.time() >= deadline:
                raise HarnessError(
                    f"timed out after {timeout_s:g}s waiting for {waiting_for} while loading "
                    f"save '{save.save_name}'" + (f" -- {hint}" if hint else ""),
                    detail={
                        "save_name": save.save_name,
                        "save_point_id": save.save_point_id,
                        "waited_s": timeout_s,
                        "last_state_table": last_states,
                        "last_transport_error": last_error,
                        # T248: so a load that failed because it could not aim a
                        # keystroke says so, instead of looking like a dead client.
                        "intro_dismiss_presses": self._intro_presses,
                        "intro_dismiss_skipped": self._intro_skipped,
                    },
                )
            await asyncio.sleep(self._poll_interval_s)

    async def _verify_far_side(self, save: SavePoint) -> None:
        """Read the loaded client's position back from ``InGame`` and compare it against what
        *save* recorded -- the assertion the whole load hangs on, on the far side of the phase
        transition, never the load call's own return value.

        Transient unreadability (a connection dropped in the rebind window, a ``pcall`` error
        while the game finishes coming up) is retried within ``verify_timeout_s``. A read that
        *succeeds* and reports the wrong position is never retried: it fails immediately
        (:meth:`_check_position`, Principle IV)."""
        client = self._client
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._verify_timeout_s
        needs_reconnect = False
        last_error: str | None = None
        while True:
            try:
                if needs_reconnect:
                    await client.reconnect()
                    needs_reconnect = False
                indices = client.state_indices
                if indices is None or indices.in_game is None:
                    raise NexusError(
                        "the InGame Lua state is not resolved on this connection",
                        detail={"reason": "in_game_state_index_unresolved"},
                    )
                result = await client.execute_command(
                    state_index=indices.in_game, lua_body=_build_verify_lua()
                )
            except (NexusError, PreflightError) as exc:
                last_error = f"{type(exc).__name__}: {exc.message}"
                needs_reconnect = True
            except OSError as exc:
                # See `_await_phase`: a raw socket error is the same retryable transport shape.
                last_error = f"{type(exc).__name__}: {exc}"
                needs_reconnect = True
            else:
                if isinstance(result, dict) and result.get("issued"):
                    self._check_position(save, result)
                    return
                lua_error = result.get("error") if isinstance(result, dict) else None
                last_error = (
                    f"the far-side read-back reported a Lua error: {lua_error!r}"
                    if lua_error
                    else f"the far-side read-back returned an unexpected shape: {result!r}"
                )
            if loop.time() >= deadline:
                raise HarnessError(
                    f"the loaded game could not be read back for verification within "
                    f"{self._verify_timeout_s:g}s of the game states reappearing, so whether "
                    f"save '{save.save_name}' actually loaded is unknown -- and unverified is "
                    "failed, never assumed loaded",
                    detail={
                        "save_name": save.save_name,
                        "save_point_id": save.save_point_id,
                        "waited_s": self._verify_timeout_s,
                        "last_error": last_error,
                    },
                )
            await asyncio.sleep(self._poll_interval_s)

    def _check_position(self, save: SavePoint, result: dict[str, Any]) -> None:
        """The position facts, checked in the order a wrong answer is most diagnostic: still in
        the front end (the load never happened) before no local player (the game is not really
        up) before the turn number (the wrong position). Each failure names what was expected
        and what was seen; none is ever retried."""
        turn = result.get("turn")
        player = result.get("local_player")
        front_end = result.get("in_front_end")
        detail: dict[str, Any] = {
            "save_name": save.save_name,
            "save_point_id": save.save_point_id,
            "expected_turn": save.turn_number,
            "observed": {"turn": turn, "local_player": player, "in_front_end": front_end},
        }
        if front_end is not False:
            raise HarnessError(
                f"after loading save '{save.save_name}' the client still reports "
                f"UI.IsInFrontEnd() = {front_end!r}; expected false -- the load never left the "
                "front end, so no game is loaded",
                detail=detail,
            )
        if not _is_plain_number(player) or player < 0:
            raise HarnessError(
                f"after loading save '{save.save_name}' the client reports no usable local "
                f"player: Game.GetLocalPlayer() = {player!r}, expected a player id >= 0",
                detail=detail,
            )
        if not _is_plain_number(turn) or turn != save.turn_number:
            raise HarnessError(
                f"the load landed on the wrong position: save '{save.save_name}' was taken at "
                f"the start of turn {save.turn_number}, but the loaded client reports "
                f"Game.GetCurrentGameTurn() = {turn!r}. A load that lands anywhere but the "
                "named save's exact position fails the operation (Principle IV), never "
                "continues silently",
                detail=detail,
            )


__all__ = [
    "DEFAULT_EXIT_TO_MENU_TIMEOUT_S",
    "DEFAULT_LOAD_TIMEOUT_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_VERIFY_TIMEOUT_S",
    "IN_GAME_STATE_NAME",
    "LuaSaveLoader",
    "MAIN_MENU_STATE_NAME",
]
