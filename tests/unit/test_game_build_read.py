"""The game-build read (R18) on a client where `Modding.GetActiveGameVersion` does not exist.

Measured on the Linux validation host (2026-09-20, Civ VI 1.0.12.9): `Modding` is absent from
`GameCore_Tuner` and `Modding.GetActiveGameVersion` is nil in `InGame`, so the declared path never
answers; the host fallback has no Linux `extract_version`; every run failed preparation with
"could not determine the client's game version". `UI.GetAppVersion()` in `InGame` answers
`"1.0.12.9 (564030)"`. These tests pin the order (declared first, UI second), the fall-through on
every failure shape, and the normalisation to the dotted form the seed-set pin compares against.
"""

from __future__ import annotations

from typing import Any

import pytest

from civsim_harness.errors import PreflightError
from civsim_harness.host.detect import OperatingSystem
from civsim_harness.observe.game_build import (
    make_tuner_version_reader,
    normalise_version,
    read_game_build,
)

GAME_CORE_TUNER, IN_GAME = 10, 132


class _Indices:
    def __init__(self, game_core_tuner: int | None, in_game: int | None) -> None:
        self.game_core_tuner = game_core_tuner
        self.in_game = in_game


class _Session:
    """Shaped like a connected NexusClient: `.state_indices` read fresh on every call."""

    def __init__(self, game_core_tuner: int | None = GAME_CORE_TUNER,
                 in_game: int | None = IN_GAME) -> None:
        self.state_indices = _Indices(game_core_tuner, in_game)


def _execute(answers: dict[int, Any]) -> Any:
    """An `execute(state_index, lua_body)` that answers per state and records what was asked."""
    calls: list[tuple[int, str]] = []

    async def execute(state_index: int, lua_body: str) -> Any:
        calls.append((state_index, lua_body))
        return answers[state_index]

    execute.calls = calls  # type: ignore[attr-defined]
    return execute


async def test_the_declared_game_core_tuner_read_is_used_when_it_answers() -> None:
    execute = _execute({GAME_CORE_TUNER: {"ok": True, "version": "1.0.12.68"}})
    read = make_tuner_version_reader(execute, game_core_tuner_state_index=_Session())

    assert await read() == "1.0.12.68"
    assert [i for i, _ in execute.calls] == [GAME_CORE_TUNER]


async def test_the_measured_ui_read_is_used_when_the_declared_call_does_not_exist() -> None:
    """The live shape: the pcall in GameCore_Tuner fails (`Modding` is nil there), and
    `UI.GetAppVersion()` in InGame answers with the build number in parentheses."""
    execute = _execute({
        GAME_CORE_TUNER: {"ok": False, "version": None},
        IN_GAME: {"ok": True, "version": "1.0.12.9 (564030)"},
    })
    read = make_tuner_version_reader(execute, game_core_tuner_state_index=_Session())

    assert await read() == "1.0.12.9"
    assert [i for i, _ in execute.calls] == [GAME_CORE_TUNER, IN_GAME]
    assert "UI.GetAppVersion()" in execute.calls[1][1]
    assert "Modding.GetActiveGameVersion()" in execute.calls[0][1]


async def test_a_version_with_no_dotted_token_is_not_a_version() -> None:
    execute = _execute({
        GAME_CORE_TUNER: {"ok": True, "version": "nil"},
        IN_GAME: {"ok": True, "version": "unknown"},
    })
    read = make_tuner_version_reader(execute, game_core_tuner_state_index=_Session())

    assert await read() is None


async def test_no_loaded_game_means_the_ui_read_is_not_reachable() -> None:
    execute = _execute({GAME_CORE_TUNER: {"ok": False, "version": None}})
    read = make_tuner_version_reader(
        execute, game_core_tuner_state_index=_Session(in_game=None)
    )

    assert await read() is None
    assert [i for i, _ in execute.calls] == [GAME_CORE_TUNER]


async def test_a_bare_resolver_cannot_reach_the_ui_read() -> None:
    execute = _execute({GAME_CORE_TUNER: {"ok": False, "version": None}})
    read = make_tuner_version_reader(execute, game_core_tuner_state_index=lambda: GAME_CORE_TUNER)

    assert await read() is None


async def test_read_game_build_composes_the_platform_and_refuses_when_nothing_answers() -> None:
    async def tuner_ok() -> str | None:
        return "1.0.12.9"

    async def tuner_none() -> str | None:
        return None

    assert await read_game_build(
        operating_system=OperatingSystem.linux, read_via_tuner=tuner_ok, read_via_host=lambda: None
    ) == "linux/1.0.12.9"
    with pytest.raises(PreflightError):
        await read_game_build(
            operating_system=OperatingSystem.linux,
            read_via_tuner=tuner_none,
            read_via_host=lambda: None,
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.0.12.9 (564030)", "1.0.12.9"),
        ("1.0.12.68 (1023995)", "1.0.12.68"),
        ("1.0.12.9", "1.0.12.9"),
        ("build 564030", None),
        ("", None),
        ("nil", None),
    ],
)
def test_normalise_version(raw: str, expected: str | None) -> None:
    assert normalise_version(raw) == expected
