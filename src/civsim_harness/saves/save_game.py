"""Save capability (T078, FR-007, FR-028, plan Complexity Tracking C1) --
**FireTuner path only. Partially complete; see "PENDING T077" below.**

*** PENDING T077 ***
Research R5's spike (T077) -- enumerating callable save paths (``Network.*``,
``UI.*``, ``Game.*``) in the ``InGame`` and ``GameCore_Tuner`` contexts
through the tuner, with the enumeration recorded as gap evidence in
``specs/002-civ-playing-harness/spikes/r5-save-path.md`` -- is running on a
separate machine against a real Civ VI client and **has not returned a
result yet**.

Under Principle II ("Firetuner-First, Skill-Extensible Harness"), a bespoke
integration path (driving the in-client Save Game dialog with synthetic
input) may exist **only** where a documented Firetuner gap proves Firetuner
cannot do the job. No such gap is documented yet, so writing
``saves/dialog_driver.py`` now -- "just in case" R5 comes back negative --
would invert the exact ordering Principle II requires and is the specific
failure that principle exists to prevent.

**Therefore this module implements only the FireTuner Lua save path**,
behind the :class:`SaveCapability` seam below, so that whichever outcome T077
records slots in without a caller-visible change:

- If R5 finds a working Lua save call, wire its confirmed Lua body into
  :data:`_SAVE_LUA_TEMPLATE` (see the ``# UNVERIFIED`` note on that
  constant) -- :class:`LuaSaveCapability` needs no other change, and T078 is
  then complete.
- If R5 instead documents a genuine Firetuner gap, a
  ``src/civsim_harness/saves/dialog_driver.py`` implementing
  :class:`SaveCapability` may be added *at that point*, with the R5
  enumeration output recorded as its
  ``IntegrationCapability.firetuner_gap`` (contracts/capability-catalog.md,
  data-model.md §11).

No bespoke/dialog-driving code exists in this module, and none should be
added before that gap is recorded on record.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from civsim_harness.errors import NexusError


class SaveCapability(Protocol):
    """The seam a save implementation must satisfy, whichever path T077
    selects for this platform.

    A bespoke implementation, if R5 documents a gap, slots in here without
    any caller-visible change -- callers depend on this ``Protocol``, never
    on a concrete class.
    """

    async def save_game(self, save_name: str) -> None:
        """Issue the named save.

        Does **not** itself verify the result on disk -- that is
        ``saves/verify.py`` (T079), deliberately kept as a separate step so a
        save's *issuance* and its *filesystem confirmation* stay two
        independently testable concerns.
        """
        ...


# UNVERIFIED (pending T077): the exact callable Lua surface for a
# named-file save in Civ VI's InGame context is exactly what the R5 spike is
# enumerating. ``Game.SaveGame`` here names the *shape* a save call should
# have, by analogy with Civ V's retired ``UI.QuickSave`` and the prior-art
# civ6-mcp project research.md cites -- it is not confirmed API, and this
# constant is written to fail closed (any Lua-side error surfaces as a
# `NexusError` below, rather than being swallowed as a false "saved"). This
# is the one place to update once T077 records its finding; nothing else in
# this module should need to change.
_SAVE_LUA_TEMPLATE = (
    'local ok, err = pcall(function() Game.SaveGame("{save_name}") end); '
    'return {{ ["saved"] = ok, ["error"] = tostring(err) }}'
)


class LuaSaveCapability:
    """FireTuner Lua save path (T078; Principle II's default/preferred path).

    Dispatches the named save directly through an already-connected Nexus
    session's ``InGame`` state -- saving is a state-mutating action, so it
    must run in ``InGame``, never ``GameCore_Tuner`` (research R3).
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
        lua_body = _SAVE_LUA_TEMPLATE.format(save_name=save_name)
        result = await self._execute(self._in_game_state_index, lua_body)
        if not isinstance(result, dict) or not result.get("saved"):
            raise NexusError(
                "FireTuner save call did not report success",
                detail={"save_name": save_name, "result": result},
            )
