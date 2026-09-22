"""Principle I: a window title reads the operator's desktop and may never be recorded.

T265, 2026-09-22. ``HostPlatform.list_window_titles`` is **the first data path in this project
that reads outside the game's own window.** Every other host capability is scoped to the Civ VI
process, its window, or the harness's own directories; this one enumerates the operator's whole
desktop -- their browser tabs, their mail client, the document they have open -- because the
content-screening gate's declared-text technique has no other source of evidence.

The boundary, stated once: **those titles feed the screening decision and nothing else.** They
may never be persisted into an agent-visible record, never enter an ``Observation``, never reach
``assemble_context``. Anything durable records a redacted form -- the gate needs to know
*whether a matching token was present*, never to keep the string.

**This file exists because a docstring saying that is worth nothing.** It is built the way
``test_parity_redteam.py`` and ``test_read_only_boundary.py`` are built: the surface is
enumerated as data, the scan is structural, and every arm has a negative control proving it can
fire. Two arms, because the leak has two shapes:

1. **Structural (AST).** A module outside ``host/`` reading ``WindowTitleListing.titles`` is
   one assignment away from a leak, and it is invisible at runtime until the assignment happens.
   So the raw titles are confined to ``host/`` by a scan, and the one production consumer is
   pinned to the tokenised form.
2. **Runtime.** The real capture path is driven with a sentinel window title on the desktop, and
   **every field of every durable record it produces** is walked for that string -- the capture
   record, its ``image_withheld``/``capture_failed`` events and their free-form ``detail``
   dicts, which is exactly where a helpful "matched on: <title>" would land.

The runtime arm scans the capture path's own records; the same sentinel is walked over a whole
turn cycle's store writes and provider requests in
``tests/integration/test_image_attachment.py::test_a_reject_category_named_on_the_desktop_
withholds_the_frame``, where the full-loop scaffold already lives.
"""

from __future__ import annotations

import ast
import io
import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from PIL import Image as PILImage

from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.host.detect import HostInfo, LinuxSessionType, OperatingSystem
from civsim_harness.host.port import (
    CaptureFrame,
    CaptureResult,
    CaptureStatus,
    GameWindow,
    WindowRect,
)
from civsim_harness.models.catalog import (
    CameraMode,
    CameraRequirements,
    CatalogVersion,
    DeclarationKind,
    ParityDeclaration,
)
from civsim_harness.models.common import (
    CapabilityId,
    DecisionStepId,
    DeclarationId,
    EventId,
    LuaContext,
    RunId,
)
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.observe.capture import capture_for_step
from civsim_harness.parity.screening import load_screening_profiles
from fakes.fake_host import DEFAULT_PROCESS, FakeHostPlatform

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HARNESS_ROOT = _REPO_ROOT / "src" / "civsim_harness"

#: The one package allowed to hold raw window titles. Everything else consumes
#: ``WindowTitleListing.text_tokens()``.
_TITLE_OWNING_PACKAGE = "host/"

#: The attribute that carries the raw strings, and the method that carries the safe form.
_RAW_TITLES_ATTRIBUTE = "titles"
_SAFE_TOKENS_METHOD = "text_tokens"

#: Where the production consumer must live -- pinned so "nobody reads .titles" cannot be
#: satisfied by nobody using the port at all (which would close image delivery while this
#: file stayed green).
_PRODUCTION_CONSUMER = "run/decision_loop.py"


# --------------------------------------------------------------------------
# ARM 1 -- structural: the raw strings never leave host/
# --------------------------------------------------------------------------


def _modules_reading_raw_titles(root: Path) -> dict[str, list[int]]:
    """Every module under *root* that reads a ``.titles`` attribute, and where."""
    found: dict[str, list[int]] = {}
    for path in sorted(root.rglob("*.py")):
        module = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == _RAW_TITLES_ATTRIBUTE
                and isinstance(node.ctx, ast.Load)
            ):
                found.setdefault(module, []).append(node.lineno)
    return found


def _modules_calling(root: Path, method: str) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(root.rglob("*.py")):
        module = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == method
            ):
                found.setdefault(module, []).append(node.lineno)
    return found


def test_raw_window_titles_never_leave_the_host_package() -> None:
    """The structural half of Principle I for T265.

    A module outside ``host/`` that reads ``listing.titles`` has the operator's desktop in a
    local variable, and from there one ``detail={...}`` puts it in the run record forever. The
    port deliberately offers a safe form (``text_tokens()``: lower-cased, unordered, with no
    association back to a window), so there is no legitimate reason for portable code to touch
    the raw strings -- and if one ever appears, it belongs in front of a human, not in a diff.
    """
    offenders = {
        module: lines
        for module, lines in _modules_reading_raw_titles(_HARNESS_ROOT).items()
        if not module.startswith(_TITLE_OWNING_PACKAGE)
    }
    assert offenders == {}, (
        "these modules outside host/ read a raw `.titles` attribute: "
        + "; ".join(f"{module}:{lines}" for module, lines in sorted(offenders.items()))
        + " -- if this is the window-title listing, consume text_tokens() instead; the raw "
        "titles are the operator's private desktop data and are bound to the screening "
        "decision alone (host/port.py::WindowTitleListing)"
    )


def test_the_production_consumer_takes_the_tokenised_form() -> None:
    """The other half, and the reason arm 1 cannot be satisfied trivially.

    "Nobody reads ``.titles``" is also true of a harness that never gathers text evidence at
    all -- which is the state this task was sent to fix, and it closes image delivery on every
    platform. So the consumer is pinned positively: the decision loop calls ``text_tokens()``.
    """
    callers = _modules_calling(_HARNESS_ROOT, _SAFE_TOKENS_METHOD)
    assert _PRODUCTION_CONSUMER in callers, (
        f"{_PRODUCTION_CONSUMER} no longer calls {_SAFE_TOKENS_METHOD}() -- either the gate's "
        "text evidence is unwired again (image delivery is then closed everywhere and "
        "test_gate_inputs.py will say so), or it is being fed from somewhere that is not the "
        "host port"
    )


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")


def test_negative_control_a_leak_outside_host_is_flagged(tmp_path: Path) -> None:
    """The control arm 1 had to pass before it could ship: the realistic leak, which is not
    someone printing a title but someone putting it in a record's ``detail`` "for debugging"."""
    _write_tree(
        tmp_path,
        {
            "host/linux/adapter.py": """\
                def list_window_titles():
                    return Listing(titles=("a window",))
                """,
            "run/decision_loop.py": """\
                def observe(ctx):
                    listing = ctx.host.list_window_titles()
                    return RunEvent(detail={"desktop": listing.titles})
                """,
        },
    )
    offenders = {
        module: lines
        for module, lines in _modules_reading_raw_titles(tmp_path).items()
        if not module.startswith(_TITLE_OWNING_PACKAGE)
    }
    assert list(offenders) == ["run/decision_loop.py"], offenders


def test_negative_control_the_host_packages_own_use_is_not_flagged(tmp_path: Path) -> None:
    """The positive twin: the adapter that produces the titles obviously reads them, and a
    scan that flagged that would be switched off within a day."""
    _write_tree(
        tmp_path,
        {
            "host/linux/adapter.py": """\
                def tokens(listing):
                    return {word for title in listing.titles for word in title.split()}
                """,
        },
    )
    offenders = {
        module: lines
        for module, lines in _modules_reading_raw_titles(tmp_path).items()
        if not module.startswith(_TITLE_OWNING_PACKAGE)
    }
    assert offenders == {}


# --------------------------------------------------------------------------
# ARM 2 -- runtime: nothing durable carries the string
# --------------------------------------------------------------------------

VIEW_DECLARATION_ID = DeclarationId("views.test_world")

#: A title no other string in this repo could produce by accident, shaped like the thing the
#: boundary actually protects: a private window nobody should ever find in a run record. It is
#: ONE word so that its raw form and its tokenised form are the same string -- the scan then
#: catches a leaked *token* as well as a leaked title.
_SENTINEL_TITLE = "zqxprivatebanking7f3a"

_WINDOW = GameWindow(
    handle=1,
    title="Sid Meier's Civilization VI (FAKE)",
    rect=WindowRect(left=0, top=0, width=8, height=8),
    pid=DEFAULT_PROCESS.pid,
)
_HOST_INFO = HostInfo(
    os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
)


def _registry_with_view() -> CapabilityRegistry:
    view = ParityDeclaration(
        declaration_id=VIEW_DECLARATION_ID,
        kind=DeclarationKind.VIEW,
        summary="Test-only world view.",
        parity_basis="Look at the world view on screen.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.views"),
        output_schema={"type": "object"},
        camera_requirements=CameraRequirements(
            mode=CameraMode.WORLD, zoom_range=(0.0, 1.0), target_must_be_revealed=False
        ),
        # T299: "platform", as every shipped view declares (catalogs/observations/views.yaml).
        # "default" resolves the strictest *union* profile on every host -- the declaration
        # T292 found screening Linux frames for Windows chrome -- and since T299 that profile
        # has no measured capture scope, so it withholds for want of coverage on any host.
        screening_profile="platform",
        introduced_in_version="test",
    )
    catalog = Catalog(
        root=Path("."),
        version=CatalogVersion(
            version="test", content_hash="test", declaration_ids=[view.declaration_id]
        ),
        declarations=MappingProxyType({view.declaration_id: view}),
        capabilities=MappingProxyType({}),
    )
    return CapabilityRegistry(catalog=catalog)


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (8, 8), (12, 44, 92)).save(buffer, format="PNG")
    return buffer.getvalue()


def _occurrences(value: Any, needle: str, *, path: str = "") -> list[str]:
    """Every place *needle* appears anywhere inside *value*, as dotted paths.

    Walks pydantic models, dataclasses, mappings and sequences alike, because the leak this
    guards against is a free-form ``detail`` dict nested inside a record, not a declared field
    of type ``str``. Returns paths rather than a bool so a failure names the field.
    """
    found: list[str] = []
    if isinstance(value, str):
        if needle in value:
            found.append(path or "<root>")
        return found
    if isinstance(value, bytes):
        if needle.encode() in value:
            found.append(path or "<root>")
        return found
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _occurrences(dump(), needle, path=path)
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_occurrences(key, needle, path=f"{path}.<key>"))
            found.extend(_occurrences(item, needle, path=f"{path}.{key}"))
        return found
    if isinstance(value, list | tuple | set | frozenset):
        for index, item in enumerate(value):
            found.extend(_occurrences(item, needle, path=f"{path}[{index}]"))
        return found
    if hasattr(value, "__dict__") and not isinstance(value, type):
        for key, item in vars(value).items():
            found.extend(_occurrences(item, needle, path=f"{path}.{key}"))
        return found
    return found


def test_negative_control_the_record_scan_finds_a_planted_title() -> None:
    """The control arm 2 had to pass before it could ship.

    The realistic leak is a title helpfully included in an event's ``detail`` -- "withheld
    because the desktop showed: <title>" -- so the control plants it exactly there, on a real
    ``RunEvent``, and the scan must name the field. A scan that could not find this one would
    pass the real test for the wrong reason forever.
    """
    planted = RunEvent(
        event_id=EventId("event-1"),
        run_id=RunId("run-control"),
        turn_number=1,
        step_index=1,
        event_type=RunEventType.IMAGE_WITHHELD,
        occurred_at=datetime(2026, 9, 22, tzinfo=UTC),
        detail={"reason": f"matched on the desktop window {_SENTINEL_TITLE!r}"},
    )

    hits = _occurrences(planted, _SENTINEL_TITLE)

    assert hits, "the record scan cannot see a title planted in a RunEvent.detail"
    assert any("detail" in hit for hit in hits), hits


def test_negative_control_the_record_scan_is_selective() -> None:
    """The positive twin, so the control above is not simply always-true."""
    clean = RunEvent(
        event_id=EventId("event-2"),
        run_id=RunId("run-control"),
        turn_number=1,
        step_index=1,
        event_type=RunEventType.IMAGE_WITHHELD,
        occurred_at=datetime(2026, 9, 22, tzinfo=UTC),
        detail={"reason": "content gate matched reject categories: ['developer_console']"},
    )
    assert _occurrences(clean, _SENTINEL_TITLE) == []


@pytest.mark.parametrize(
    "desktop",
    [
        pytest.param([_WINDOW.title, _SENTINEL_TITLE], id="clean-frame-delivered"),
        pytest.param(
            [_WINDOW.title, _SENTINEL_TITLE, "Developer Console"], id="frame-withheld-by-the-gate"
        ),
    ],
)
def test_no_window_title_reaches_any_durable_record_the_capture_path_produces(
    desktop: list[str],
) -> None:
    """The runtime half, driven through the real production capture path.

    The host reports a desktop with a private-looking window on it; the loop's own expression
    (``host.list_window_titles().text_tokens()``) turns it into the gate's evidence; the real
    ``capture_for_step`` screens the frame and produces the records the run loop persists. Both
    outcomes are exercised, because they take different code paths and the *withheld* one is
    where a leak is most tempting: that path writes a human-readable reason explaining what the
    gate matched, and "what it matched" is a hair's breadth from "the text it matched on".
    """
    host = FakeHostPlatform()
    host.set_window_titles(desktop)
    host.set_capture_result(
        CaptureResult(
            status=CaptureStatus.ok,
            frame=CaptureFrame(
                width=8, height=8, rect=_WINDOW.rect, image_bytes=_png_bytes(), image_format="PNG"
            ),
        )
    )

    # Exactly the expression run/decision_loop.py uses -- not a re-derivation of it.
    detected_text_tokens = host.list_window_titles().text_tokens()
    assert detected_text_tokens is not None and _SENTINEL_TITLE in detected_text_tokens, (
        "the sentinel is not even in the evidence, so this test would pass without proving "
        "anything about where the evidence can travel"
    )

    result = capture_for_step(
        host=host,
        host_info=_HOST_INFO,
        window=_WINDOW,
        view_declaration_id=VIEW_DECLARATION_ID,
        camera_state={"mode": "world", "zoom": 0.5},
        run_id=RunId("run-title-boundary"),
        turn_number=3,
        decision_step_id=DecisionStepId("step-7"),
        step_index=7,
        captured_at=datetime(2026, 9, 22, tzinfo=UTC),
        registry=_registry_with_view(),
        profiles=load_screening_profiles(),
        detected_text_tokens=detected_text_tokens,
    )

    leaks = _occurrences(result.capture, _SENTINEL_TITLE, path="capture")
    for index, event in enumerate(result.events):
        leaks.extend(_occurrences(event, _SENTINEL_TITLE, path=f"events[{index}]"))
    assert leaks == [], (
        f"a window title reached a durable record: {leaks}. It must be redacted or hashed "
        "there, never recorded verbatim -- the gate's decision needs to know whether a "
        "matching token was present, not to keep the operator's window title"
    )

    # Belt and braces: whatever the walker might not reach, the JSON the store would write does.
    serialised = json.dumps(
        {
            "capture": result.capture.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in result.events],
        }
    )
    assert _SENTINEL_TITLE not in serialised
