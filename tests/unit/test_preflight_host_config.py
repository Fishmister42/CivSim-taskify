"""T204 hardening items 1 and 2 -- host-config preflight checks (`run/preparation.py`).

1. :func:`~civsim_harness.run.preparation.debug_menu_preflight` -- reads ``EnableDebugMenu`` from
   ``AppOptions.txt`` and *records* it. Never refuses a run over it: a live-client spike
   (``specs/002-civ-playing-harness/spikes/principle-i-debugmenu-linux.md``) found the tuner's own
   callable surface byte-identical with the debug menu on and off across three separate
   comparisons, so there is no parity basis for treating this as a gate.
2. :func:`~civsim_harness.run.preparation.turn_timer_preflight` -- verifies, via an injected
   reader, that this run is not using a turn timer. A live, measured finding
   (``GameConfiguration.GetTurnTimerType()`` returning ``TURNTIMER_STANDARD`` in an ordinary
   single-player game, turns advancing 1 -> 6 with zero input once one end-turn was issued) makes
   this a hard preflight gate: verified-none proceeds, verified-active refuses the run outright
   (FR-011, FR-014, FR-015/SC-022 would otherwise all be silently corrupted), and undeterminable
   proceeds but records an explicit *unverified* precondition rather than silently assuming safe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from civsim_harness.errors import PreflightError
from civsim_harness.host.port import GameDirectories
from civsim_harness.run.preparation import (
    DebugMenuPreflightResult,
    DebugMenuState,
    TurnTimerPreconditionState,
    TurnTimerReading,
    TurnTimerReadStatus,
    debug_menu_preflight,
    turn_timer_preflight,
)
from fakes.fake_host import FakeHostPlatform

# --------------------------------------------------------------------------
# Hardening item 1 -- EnableDebugMenu: present (on/off), absent, unreadable
# --------------------------------------------------------------------------


def _host_with_app_options(
    tmp_path: Path, *, contents: str | None
) -> tuple[FakeHostPlatform, Path]:
    """A `FakeHostPlatform` whose `resolve_game_directories()` points at a real, on-disk
    `AppOptions.txt` under *tmp_path* -- `contents=None` leaves the file (and its directory)
    entirely absent, exactly the "fresh install" case `debug_menu_preflight`'s own docstring
    names.
    """
    saves_dir = tmp_path / "Saves" / "Single"
    app_options_path = tmp_path / "AppOptions.txt"
    if contents is not None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        app_options_path.write_text(contents, encoding="utf-8")
    host = FakeHostPlatform()
    host.set_directories(GameDirectories(saves_dir=saves_dir, app_options_path=app_options_path))
    return host, app_options_path


def test_debug_menu_preflight_present_enabled(tmp_path: Path) -> None:
    host, path = _host_with_app_options(
        tmp_path, contents="EnableTuner 1\nEnableDebugMenu 1    # [Debug]\n"
    )
    result = debug_menu_preflight(host)
    assert result == DebugMenuPreflightResult(
        state=DebugMenuState.ENABLED, app_options_path=path
    )


def test_debug_menu_preflight_present_disabled(tmp_path: Path) -> None:
    host, path = _host_with_app_options(
        tmp_path, contents="EnableTuner 1        # [Debug]\nEnableDebugMenu 0    # [Debug]\n"
    )
    result = debug_menu_preflight(host)
    assert result == DebugMenuPreflightResult(
        state=DebugMenuState.DISABLED, app_options_path=path
    )


def test_debug_menu_preflight_absent_file_is_unknown_not_a_crash(tmp_path: Path) -> None:
    """The client rewrites `AppOptions.txt` on exit and its directory need not pre-exist on a
    fresh install (the same accommodation `host._shared.read_disk_space` already needed) -- a
    missing file must never raise out of preflight."""
    missing_root = tmp_path / "fresh-install-never-launched"
    host, path = _host_with_app_options(missing_root, contents=None)
    assert not path.exists()

    result = debug_menu_preflight(host)

    assert result.state is DebugMenuState.UNKNOWN
    assert result.app_options_path == path
    assert result.detail is not None
    assert "does not exist" in result.detail


def test_debug_menu_preflight_present_but_no_entry_is_unknown(tmp_path: Path) -> None:
    host, path = _host_with_app_options(tmp_path, contents="EnableTuner 1\nPlayIntroVideo 0\n")
    result = debug_menu_preflight(host)
    assert result.state is DebugMenuState.UNKNOWN
    assert result.detail is not None
    assert "no EnableDebugMenu entry" in result.detail


def test_debug_menu_preflight_unreadable_file_is_unknown_not_a_crash(tmp_path: Path) -> None:
    """A file that exists but cannot be read as text (simulated here with a directory in its
    place, which raises an `OSError` on every platform when opened for reading) must be reported
    the same way as a missing file -- `UNKNOWN`, never raised."""
    root = tmp_path / "unreadable-host"
    root.mkdir()
    app_options_path = root / "AppOptions.txt"
    app_options_path.mkdir()  # a directory where a file is expected -- unreadable as text
    host = FakeHostPlatform()
    host.set_directories(
        GameDirectories(saves_dir=root / "Saves" / "Single", app_options_path=app_options_path)
    )

    result = debug_menu_preflight(host)

    assert result.state is DebugMenuState.UNKNOWN
    assert result.app_options_path == app_options_path
    assert result.detail is not None
    assert "could not be read" in result.detail


def test_debug_menu_preflight_never_raises_regardless_of_value() -> None:
    """Structural guard: whatever `AppOptions.txt` says, `debug_menu_preflight` only ever
    *reports* -- it must never refuse a run over this setting (no parity basis for a gate, per the
    live spike's own evidence). Walks the actual AST (not the raw source text, which would
    false-positive on the word "raise" appearing in the function's own docstring) for a `raise`
    statement anywhere in the function body."""
    import ast
    import inspect
    import textwrap

    from civsim_harness.run import preparation

    source = textwrap.dedent(inspect.getsource(preparation.debug_menu_preflight))
    tree = ast.parse(source)
    raises = [node for node in ast.walk(tree) if isinstance(node, ast.Raise)]
    assert raises == []


def test_debug_menu_preflight_uses_the_same_app_options_path_as_enable_tuner(
    tmp_path: Path,
) -> None:
    """`EnableDebugMenu` is read from the exact path `resolve_game_directories()` resolves --
    the same file `EnableTuner` already lives in (research R1) -- never a path this function
    invents on its own."""
    host, path = _host_with_app_options(tmp_path, contents="EnableDebugMenu 0\n")
    result = debug_menu_preflight(host)
    assert result.app_options_path == host.resolve_game_directories().app_options_path
    assert result.app_options_path == path


# --------------------------------------------------------------------------
# Hardening item 2 -- turn-timer preflight: verified-off, verified-on, undeterminable
# --------------------------------------------------------------------------


def test_turn_timer_preflight_verified_none_proceeds() -> None:
    reading = TurnTimerReading(
        status=TurnTimerReadStatus.DETERMINED,
        turn_timer_type="TURNTIMER_NONE",
        turn_timer_hash=-1525060181,
    )
    result = turn_timer_preflight(read_turn_timer=lambda: reading)
    assert result.state is TurnTimerPreconditionState.VERIFIED_NONE
    assert result.turn_timer_type == "TURNTIMER_NONE"
    assert result.turn_timer_hash == -1525060181


def test_turn_timer_preflight_accepts_the_other_confirmed_no_timer_name() -> None:
    """`TURNTIMER_NONE` and `NO_TURNTIMER` are distinct, build-dependent hashes on the probed
    build -- both must be accepted since which name a given build actually uses is not assumed
    here (the live finding's own instruction: "accept either if you cannot tell")."""
    reading = TurnTimerReading(
        status=TurnTimerReadStatus.DETERMINED,
        turn_timer_type="NO_TURNTIMER",
        turn_timer_hash=-1206781825,
    )
    result = turn_timer_preflight(read_turn_timer=lambda: reading)
    assert result.state is TurnTimerPreconditionState.VERIFIED_NONE
    assert result.turn_timer_type == "NO_TURNTIMER"


def test_turn_timer_preflight_verified_active_refuses_the_run() -> None:
    """FR-011/FR-014/FR-015/SC-022: a run started with a turn timer active would produce a
    clean-looking but meaningless dataset -- this must fail the run, not merely record it."""
    reading = TurnTimerReading(
        status=TurnTimerReadStatus.DETERMINED,
        turn_timer_type="TURNTIMER_STANDARD",
        turn_timer_hash=2133509568,
    )
    with pytest.raises(PreflightError) as excinfo:
        turn_timer_preflight(read_turn_timer=lambda: reading)
    assert excinfo.value.detail["turn_timer_type"] == "TURNTIMER_STANDARD"
    assert excinfo.value.detail["turn_timer_hash"] == 2133509568


@pytest.mark.parametrize(
    "turn_timer_type,turn_timer_hash",
    [
        ("TURNTIMER_STANDARD", 2133509568),
        ("TURNTIMER_DYNAMIC", 698670180),
        ("TURNTIMER_FIXED", 1033907547),
    ],
)
def test_turn_timer_preflight_refuses_every_active_timer_type(
    turn_timer_type: str, turn_timer_hash: int
) -> None:
    reading = TurnTimerReading(
        status=TurnTimerReadStatus.DETERMINED,
        turn_timer_type=turn_timer_type,
        turn_timer_hash=turn_timer_hash,
    )
    with pytest.raises(PreflightError):
        turn_timer_preflight(read_turn_timer=lambda: reading)


def test_turn_timer_preflight_undeterminable_records_unverified_not_silently_pass() -> None:
    """The reader could not resolve an answer at all -- this must **not** raise, and must **not**
    default to treating the precondition as satisfied (that is the exact assumption the retracted
    auto-end-turn finding showed is dangerous); it must be recorded as explicitly unverified."""
    reading = TurnTimerReading(
        status=TurnTimerReadStatus.UNDETERMINABLE,
        reason="GameConfiguration is not available before a game is loaded",
    )
    result = turn_timer_preflight(read_turn_timer=lambda: reading)
    assert result.state is TurnTimerPreconditionState.UNVERIFIED
    assert result.turn_timer_type is None
    assert result.turn_timer_hash is None
    assert result.reason == "GameConfiguration is not available before a game is loaded"


def test_turn_timer_reading_requires_a_reason_when_undeterminable() -> None:
    with pytest.raises(ValueError):
        TurnTimerReading(status=TurnTimerReadStatus.UNDETERMINABLE, reason=None)


def test_turn_timer_reading_requires_a_type_when_determined() -> None:
    with pytest.raises(ValueError):
        TurnTimerReading(status=TurnTimerReadStatus.DETERMINED, turn_timer_type=None)


def test_turn_timer_preflight_never_defaults_to_assuming_no_timer() -> None:
    """Structural guard against the exact failure mode this hardening item exists to prevent: no
    code path in `turn_timer_preflight` may construct a `VERIFIED_NONE` result without the
    injected reader itself having reported `DETERMINED` with a no-timer name. Walks the actual
    AST (not the raw source text, which would false-positive on "VERIFIED_NONE" appearing in the
    function's own docstring) for every `TurnTimerPreconditionState.VERIFIED_NONE` attribute
    access in the function body -- there must be exactly one, and it must sit inside the `if`
    branch that checks `reading.turn_timer_type in _NO_TIMER_NAMES`.
    """
    import ast
    import inspect
    import textwrap

    from civsim_harness.run import preparation

    source = textwrap.dedent(inspect.getsource(preparation.turn_timer_preflight))
    tree = ast.parse(source)
    verified_none_refs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "VERIFIED_NONE"
    ]
    assert len(verified_none_refs) == 1

    # And that one reference is reached only from inside an `if` statement whose test mentions
    # `_NO_TIMER_NAMES` -- i.e. it is not a bare/default construction reachable unconditionally.
    guarding_ifs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and "_NO_TIMER_NAMES" in ast.dump(node.test)
    ]
    assert len(guarding_ifs) == 1
    guard = guarding_ifs[0]
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "VERIFIED_NONE" for n in ast.walk(guard)
    )
