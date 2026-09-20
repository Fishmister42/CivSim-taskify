"""Save capability (T078, FR-007, FR-028, plan Complexity Tracking C1) --
**FireTuner path, verified against a real Civilization VI client.**

Research R5's spike (T077) enumerated callable save paths (``Network.*``,
``UI.*``, ``Game.*``) in the ``InGame`` and ``GameCore_Tuner`` contexts
against a real, running client (Linux, native Aspyr build). Its findings are
recorded in ``specs/002-civ-playing-harness/spikes/r5-save-path.md``, with
raw probe output under ``specs/002-civ-playing-harness/spikes/r5-raw/``. The
result is **Outcome A**: ``Network.SaveGame(gameFile)``, called in the
``InGame`` context, writes a real, named ``.Civ6Save`` to disk. Four
consecutive calls in the live test produced four valid, size-stable files
with zero failures.

Two things the spike also nailed down, both reflected below:

- ``Network``, ``UI``, and ``UIManager`` are entirely absent from
  ``GameCore_Tuner`` -- every save candidate errors at the root there. The
  save call **must** run in ``InGame``; declaring it under
  ``GameCore_Tuner`` is a catalog-load error by design (research R3).
- ``Network.SaveGame`` returns ``true`` **immediately**, but the file lands
  on disk **asynchronously** -- the return value is evidence the call did
  not error, never proof the save exists. That is exactly why this module's
  own :meth:`LuaSaveCapability.save_game` only reports that the call was
  *issued* without a Lua-side error, and never claims the save is
  confirmed -- filesystem confirmation is ``saves/verify.py`` (T079)'s job,
  deliberately kept a separate, independently testable step.

**Under Principle II ("Firetuner-First, Skill-Extensible Harness")**, this
positive result means the bespoke Save Game dialog driver
(``src/civsim_harness/saves/dialog_driver.py``, driving the in-client Save
Game dialog with synthetic input) is now **forbidden, not merely
unnecessary** -- a working Firetuner path exists, so there is no Firetuner
gap left to justify a bespoke path, and none should be written. This module
implements only the FireTuner Lua save path, behind the
:class:`SaveCapability` seam below, exactly as it did while R5 was pending --
the seam cost nothing to keep and means a caller-visible change was never
needed either way.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from civsim_harness.errors import NexusError


class SaveCapability(Protocol):
    """The seam a save implementation must satisfy.

    Callers depend on this ``Protocol``, never on :class:`LuaSaveCapability`
    directly, so a different implementation could still slot in here without
    any caller-visible change -- though, per this module's docstring, no
    such need exists today: the FireTuner Lua path is verified and Principle
    II forbids a bespoke alternative while it holds.
    """

    async def save_game(self, save_name: str) -> None:
        """Issue the named save.

        Does **not** itself verify the result on disk -- that is
        ``saves/verify.py`` (T079), deliberately kept as a separate step so a
        save's *issuance* and its *filesystem confirmation* stay two
        independently testable concerns.
        """
        ...


# VERIFIED against a real Civilization VI client (T077 spike; see this
# module's docstring). ``SaveLocations.LOCAL_STORAGE`` and
# ``SaveTypes.SINGLE_PLAYER`` both evaluate to ``1`` in the live client and
# are passed through their named constants (never the bare literal) so a
# reader does not have to take that mapping on faith. An arbitrary ``Name``
# is honoured verbatim as the filename stem -- the live test wrote
# ``civsim__spike__t0001.Civ6Save`` from exactly this shape of call -- so
# ``save_point.save_name_for``'s ``civsim__<run_id>__t<turn:04d>`` convention
# needs no escaping here.
#
# The call is wrapped in ``pcall`` so a Lua-side error (e.g. a future client
# build removing/renaming ``Network.SaveGame``) surfaces as a reported
# failure rather than an unhandled error killing the tuner connection. The
# ``["issued"]`` key name is deliberate, not ``"saved"``: ``pcall`` returning
# true only means the call did not error -- per the spike, the file itself
# is written asynchronously, so this can never claim more than "issued".
#
# ``(ok and "") or tostring(err)`` rather than ``ok and nil or tostring(err)``:
# the latter is the classic Lua ``and/or`` ternary trap -- when *ok* is true
# the middle operand would be ``nil``, which is itself falsy, so the ``or``
# branch would fire anyway and always report ``tostring(err)`` regardless of
# *ok*. Using the always-truthy ``""`` as the "no error" value sidesteps that
# trap; pcall's own ``err`` is ``nil`` on success in any case, since the
# wrapped function returns nothing.
_SAVE_LUA_TEMPLATE = (
    "local gameFile = {{}}; "
    'gameFile.Name = "{save_name}"; '
    "gameFile.Location = SaveLocations.LOCAL_STORAGE; "
    "gameFile.Type = SaveTypes.SINGLE_PLAYER; "
    "gameFile.IsAutosave = false; "
    "gameFile.IsQuicksave = false; "
    "local ok, err = pcall(function() Network.SaveGame(gameFile) end); "
    'return {{ ["issued"] = ok, ["error"] = (ok and "") or tostring(err) }}'
)


class LuaSaveCapability:
    """FireTuner Lua save path (T078; Principle II's default/preferred path).

    Dispatches the named save directly through an already-connected Nexus
    session's ``InGame`` state -- saving is a state-mutating action, so it
    must run in ``InGame``, never ``GameCore_Tuner`` (research R3, and now
    directly confirmed by the T077 spike: ``Network`` does not exist at all
    in ``GameCore_Tuner``).
    """

    def __init__(
        self,
        execute: Callable[[int, str], Awaitable[Any]],
        *,
        in_game_state_index: int,
    ) -> None:
        """*execute* is typically
        :meth:`civsim_harness.nexus.client.NexusClient.execute_command`
        called as ``execute(state_index, lua_body)`` -- accepted as a plain
        callable, matching ``observe.game_build.make_tuner_version_reader``,
        so this module has no hard dependency on the transport for its own
        logic.
        """
        self._execute = execute
        self._in_game_state_index = in_game_state_index

    async def save_game(self, save_name: str) -> None:
        """Issue *save_name* as a FireTuner ``Network.SaveGame`` call.

        Raises :class:`~civsim_harness.errors.NexusError` when the Lua call
        itself reported an error (``pcall`` failed). A successful return
        here means only that the call was *issued* without a Lua-side error
        -- per the T077 spike, ``Network.SaveGame`` returns immediately
        while the file is still written asynchronously, so this is never
        treated as proof the save landed on disk. Call ``saves/verify.py``'s
        ``verify_save`` afterward for that confirmation.
        """
        lua_body = _SAVE_LUA_TEMPLATE.format(save_name=save_name)
        result = await self._execute(self._in_game_state_index, lua_body)
        if not isinstance(result, dict) or not result.get("issued"):
            raise NexusError(
                "FireTuner save call did not report success",
                detail={"save_name": save_name, "result": result},
            )
