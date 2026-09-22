"""The Linux X11 window walk, pinned -- ``find_game_window`` and the desktop enumeration.

**Why this file exists (T265, 2026-09-22).** The content gate's declared-text technique needs
*desktop-wide* window titles, and the only `_NET_CLIENT_LIST` walk in this repo was private
inside ``LinuxHostPlatform.find_game_window`` -- the function every capture on this host depends
on. Extracting that walk so it can also enumerate the desktop is exactly the kind of refactor
that is "obviously behaviour-preserving" right up until it is not, and the live lane made a
behaviour pin its one condition on the grant. So these tests were written and run **against the
pre-extraction code first**, then again after: same assertions, same results.

Nothing here touches a real X server. ``find_game_window`` does ``from Xlib import X`` and
``from Xlib.display import Display`` *inside* the function, so scripted stand-ins installed in
``sys.modules`` are what it finds and no connection is ever opened -- which is also why this
file is not ``@pytest.mark.client``: it cannot reach the operator's display, the client, or the
tuner, and it runs on a machine with no python-xlib at all. The fake models the X calls the
adapter actually makes, and records every property read, so "which windows did the walk touch"
is assertable rather than assumed -- the laziness of the original loop (it stops at the first
pid match and never looks at a window after it) is a real behaviour, and an eager rewrite would
be a change even though every passing case still returns the same window.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from civsim_harness.host.detect import LinuxSessionType
from civsim_harness.host.port import GameProcess, GameWindow, WindowRect

_GAME_PID = 4242
_OTHER_PID = 99


class _FakeProperty:
    def __init__(self, value: Any) -> None:
        self.value = value


class _FakeGeometry:
    def __init__(self, x: int, y: int, width: int, height: int) -> None:
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class _FakeCoords:
    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y


class _FakeWindowResource:
    """One X window, as the adapter uses it: two property reads and a geometry read."""

    def __init__(self, server: _FakeXServer, window_id: int) -> None:
        self._server = server
        self._window_id = window_id

    def get_full_property(self, atom: str, _kind: object) -> _FakeProperty | None:
        self._server.note_read(self._window_id, atom)
        spec = self._server.windows[self._window_id]
        if atom == "_NET_WM_PID":
            pid = spec.get("pid")
            return None if pid is None else _FakeProperty([pid])
        if atom == "_NET_WM_NAME":
            title = spec.get("title")
            return None if title is None else _FakeProperty(title.encode("utf-8"))
        if atom == "WM_NAME":
            legacy = spec.get("legacy_title")
            return None if legacy is None else _FakeProperty(legacy.encode("utf-8"))
        return None

    def get_geometry(self) -> _FakeGeometry:
        self._server.note_read(self._window_id, "GEOMETRY")
        spec = self._server.windows[self._window_id]
        # A reparenting WM reports (0, 0) for a managed window; the absolute position
        # comes from translate_coords. Modelled faithfully so the translation is exercised.
        return _FakeGeometry(0, 0, spec["width"], spec["height"])


class _FakeRoot(_FakeWindowResource):
    def get_full_property(self, atom: str, kind: object) -> _FakeProperty | None:
        if atom == "_NET_CLIENT_LIST":
            if self._server.client_list is None:
                return None
            return _FakeProperty(list(self._server.client_list))
        return super().get_full_property(atom, kind)

    def translate_coords(self, candidate: _FakeWindowResource, x: int, y: int) -> _FakeCoords:
        window_id = candidate._window_id  # noqa: SLF001 - the fake's own internals
        self._server.note_read(window_id, "TRANSLATE")
        spec = self._server.windows[window_id]
        return _FakeCoords(spec["origin"][0] + x, spec["origin"][1] + y)


class _FakeScreen:
    def __init__(self, root: _FakeRoot) -> None:
        self.root = root


class _FakeXServer:
    """A scripted desktop. Records every read so the walk's *reach* is assertable."""

    def __init__(
        self,
        windows: dict[int, dict[str, Any]],
        *,
        client_list: list[int] | None,
        poisoned: frozenset[int] = frozenset(),
    ) -> None:
        self.windows = windows
        self.client_list = client_list
        self.poisoned = poisoned
        self.reads: list[tuple[int, str]] = []
        self.closed = 0
        self._root = _FakeRoot(self, 0)

    def note_read(self, window_id: int, what: str) -> None:
        if window_id in self.poisoned:
            raise RuntimeError(f"window {window_id} vanished (poisoned by this test)")
        self.reads.append((window_id, what))

    # -- the Display surface the adapter uses -------------------------------

    def screen(self) -> _FakeScreen:
        return _FakeScreen(self._root)

    def intern_atom(self, name: str) -> str:
        return name

    def create_resource_object(self, kind: str, window_id: int) -> _FakeWindowResource:
        assert kind == "window"
        return _FakeWindowResource(self, int(window_id))

    def close(self) -> None:
        self.closed += 1


def _install(monkeypatch: pytest.MonkeyPatch, server: _FakeXServer) -> None:
    """Install the scripted server in place of ``python-xlib``. No socket is ever opened.

    Fake modules are pushed into ``sys.modules`` under the names the adapter imports rather
    than monkeypatching the real ``Xlib.display.Display``, for two reasons: this file must not
    import an OS-specific library (the repo bans that outside ``host/`` -- research R19, and
    ``tests/unit/test_platform_neutrality.py`` enforces it), and the adapter's window walk
    must be exercisable on a machine where python-xlib is not installed at all. Same idiom as
    the win32 fakes in ``tests/contract/test_host_platform_port.py``: the adapter's
    ``from Xlib import X`` / ``from Xlib.display import Display`` run at call time and find
    whatever is already in ``sys.modules``.
    """
    fake_x = SimpleNamespace(AnyPropertyType=0)
    fake_display_module = SimpleNamespace(Display=lambda *a, **k: server)
    monkeypatch.setitem(sys.modules, "Xlib", SimpleNamespace(X=fake_x))
    monkeypatch.setitem(sys.modules, "Xlib.X", fake_x)
    monkeypatch.setitem(sys.modules, "Xlib.display", fake_display_module)


def _adapter() -> Any:
    from civsim_harness.host.linux.adapter import LinuxHostPlatform

    return LinuxHostPlatform(session_type=LinuxSessionType.x11)


def _desktop() -> _FakeXServer:
    """The ordinary case: a panel, the game, and a browser after it."""
    return _FakeXServer(
        {
            11: {"pid": _OTHER_PID, "title": "Top Panel", "width": 1920, "height": 40,
                 "origin": (0, 0)},
            22: {"pid": _GAME_PID, "title": "Sid Meier's Civilization VI (DX12)",
                 "width": 1920, "height": 1200, "origin": (2560, 0)},
            33: {"pid": 77, "title": "a browser", "width": 800, "height": 600,
                 "origin": (10, 10)},
        },
        client_list=[11, 22, 33],
    )


# --------------------------------------------------------------------------
# The pin: find_game_window's behaviour, unchanged by the T265 extraction.
# --------------------------------------------------------------------------


def test_find_game_window_returns_the_matching_window_with_root_space_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _desktop()
    _install(monkeypatch, server)

    window = _adapter().find_game_window(GameProcess(pid=_GAME_PID, name="Civ6"))

    assert window == GameWindow(
        handle=22,
        title="Sid Meier's Civilization VI (DX12)",
        rect=WindowRect(left=2560, top=0, width=1920, height=1200),
        pid=_GAME_PID,
    )
    assert server.closed == 1


def test_find_game_window_stops_at_the_first_match_and_never_touches_a_later_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The laziness is load-bearing, not incidental.

    The original loop returns the instant a pid matches, so a window *after* the game in
    ``_NET_CLIENT_LIST`` is never read at all -- and a window that vanishes between the list
    read and a property read raises ``BadWindow``. An extraction that eagerly enumerated the
    whole desktop first would turn a successful capture into an exception on exactly the kind
    of desktop this harness runs on (a browser closing mid-run). Poisoning window 33 makes
    that difference a failure rather than a footnote.
    """
    desktop = _desktop()
    server = _FakeXServer(desktop.windows, client_list=[11, 22, 33], poisoned=frozenset({33}))
    _install(monkeypatch, server)

    window = _adapter().find_game_window(GameProcess(pid=_GAME_PID, name="Civ6"))

    assert window is not None and window.handle == 22
    assert {window_id for window_id, _ in server.reads} == {11, 22}


def test_find_game_window_reads_only_the_pid_of_a_non_matching_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-matching window costs exactly one property read -- no title, no geometry.

    This is the other half of the laziness pin, and the one an extraction that yields
    ``(id, pid, title)`` eagerly would break: it would read every preceding window's title too.
    """
    server = _desktop()
    _install(monkeypatch, server)

    _adapter().find_game_window(GameProcess(pid=_GAME_PID, name="Civ6"))

    assert [what for window_id, what in server.reads if window_id == 11] == ["_NET_WM_PID"]


def test_find_game_window_returns_none_when_no_window_belongs_to_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _desktop()
    _install(monkeypatch, server)

    assert _adapter().find_game_window(GameProcess(pid=123_456, name="Civ6")) is None
    assert server.closed == 1


def test_find_game_window_returns_none_when_the_client_list_property_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _FakeXServer({}, client_list=None)
    _install(monkeypatch, server)

    assert _adapter().find_game_window(GameProcess(pid=_GAME_PID, name="Civ6")) is None
    assert server.closed == 1


def test_find_game_window_tolerates_a_window_with_no_pid_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _FakeXServer(
        {
            5: {"pid": None, "title": "no pid here", "width": 100, "height": 100,
                "origin": (0, 0)},
            22: {"pid": _GAME_PID, "title": "Civ VI", "width": 640, "height": 480,
                 "origin": (1, 2)},
        },
        client_list=[5, 22],
    )
    _install(monkeypatch, server)

    window = _adapter().find_game_window(GameProcess(pid=_GAME_PID, name="Civ6"))

    assert window is not None and window.handle == 22
    assert window.rect == WindowRect(left=1, top=2, width=640, height=480)


def test_find_game_window_yields_an_empty_title_when_the_name_property_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned because an empty title is *load-bearing downstream*: screening's source gate
    rejects a window with no plausible title identity, so this case must keep producing a
    ``GameWindow`` with ``title=""`` rather than ``None`` or a fabricated name."""
    server = _FakeXServer(
        {22: {"pid": _GAME_PID, "title": None, "width": 8, "height": 8, "origin": (0, 0)}},
        client_list=[22],
    )
    _install(monkeypatch, server)

    window = _adapter().find_game_window(GameProcess(pid=_GAME_PID, name="Civ6"))

    assert window is not None and window.title == ""


def test_find_game_window_on_wayland_reports_no_window_without_opening_a_display() -> None:
    from civsim_harness.host.linux.adapter import LinuxHostPlatform

    adapter = LinuxHostPlatform(session_type=LinuxSessionType.wayland)
    assert adapter.find_game_window(GameProcess(pid=_GAME_PID, name="Civ6")) is None


# --------------------------------------------------------------------------
# The new half: the desktop listing the content gate's text evidence comes from.
# --------------------------------------------------------------------------


def test_list_window_titles_enumerates_the_whole_desktop_not_just_the_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _desktop()
    _install(monkeypatch, server)

    listing = _adapter().list_window_titles()

    assert listing.available is True
    assert listing.titles == (
        "Top Panel",
        "Sid Meier's Civilization VI (DX12)",
        "a browser",
    )
    assert server.closed == 1


def test_list_window_titles_tokenises_into_the_content_gates_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate matches a reject id's own ``_``-split tokens against this set
    (``parity/screening.py``), so the token form has to be lower-cased, unpunctuated words."""
    server = _FakeXServer(
        {7: {"pid": 1, "title": "FireTuner - Civilization VI", "width": 1, "height": 1,
             "origin": (0, 0)}},
        client_list=[7],
    )
    _install(monkeypatch, server)

    tokens = _adapter().list_window_titles().text_tokens()

    assert tokens == frozenset({"firetuner", "civilization", "vi"})


def test_list_window_titles_falls_back_to_the_legacy_wm_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _FakeXServer(
        {7: {"pid": 1, "title": None, "legacy_title": "Developer Console", "width": 1,
             "height": 1, "origin": (0, 0)}},
        client_list=[7],
    )
    _install(monkeypatch, server)

    listing = _adapter().list_window_titles()

    assert listing.titles == ("Developer Console",)
    # The whole point: this one *does* match a shipped reject id, so the technique fires.
    assert {"developer", "console"} <= (listing.text_tokens() or frozenset())


def test_list_window_titles_reports_unavailable_when_the_walk_breaks_partway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial listing is the fail-open shape: it would tell the gate a source ran and the
    desktop was clean, when the window that mattered is the one that was missed."""
    desktop = _desktop()
    server = _FakeXServer(desktop.windows, client_list=[11, 22, 33], poisoned=frozenset({33}))
    _install(monkeypatch, server)

    listing = _adapter().list_window_titles()

    assert listing.available is False
    assert listing.titles == ()
    assert listing.text_tokens() is None
    assert "incomplete" in listing.reason
    assert server.closed == 1


def test_list_window_titles_reports_an_empty_desktop_as_available_with_no_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"The source ran and found nothing" is a *different* answer from "no source ran", and
    only the second one closes image delivery."""
    server = _FakeXServer({}, client_list=[])
    _install(monkeypatch, server)

    listing = _adapter().list_window_titles()

    assert listing.available is True
    assert listing.titles == ()
    assert listing.text_tokens() == frozenset()


def test_list_window_titles_on_wayland_reports_unavailable_with_a_reason() -> None:
    from civsim_harness.host.linux.adapter import LinuxHostPlatform

    listing = LinuxHostPlatform(session_type=LinuxSessionType.wayland).list_window_titles()

    assert listing.available is False
    assert listing.text_tokens() is None
    assert "wayland" in listing.reason.lower()


def test_list_window_titles_never_puts_a_title_in_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Principle I, at the one place a title could plausibly be written down by accident:
    ``reason`` is recorded on withheld captures and run events, so it carries counts only."""
    server = _FakeXServer(
        {7: {"pid": 1, "title": "Matt's private banking tab", "width": 1, "height": 1,
             "origin": (0, 0)}},
        client_list=[7],
    )
    _install(monkeypatch, server)

    listing = _adapter().list_window_titles()

    assert "banking" not in listing.reason
    assert "1 top-level window" in listing.reason
