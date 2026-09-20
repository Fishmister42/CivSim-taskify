"""Unit test: platform neutrality of the portable core (T054, research R19).

Walks every module under `civsim_harness` **except** `civsim_harness.host`
(and its subpackages), imports each one, and asserts two things ruff's
TID251 banned-api rule is meant to guarantee at lint time -- enforced here
at test time too, per plan.md's Testing table ("a matrix build ... catches
an accidental OS-specific import"):

1. Importing the module never pulls a platform-bound package (`pywin32`
   and friends, `winsdk`, `pydirectinput`, `Quartz`, `AppKit`, `Xlib`,
   `dbus`) into `sys.modules`.
2. The module's own source text never references `windll` (as in
   `ctypes.windll.X`). This closes a gap the task brief names explicitly:
   TID251 only matches literal `import` statements, so attribute access on
   a plain `import ctypes` slips past lint. `host/` itself is exempted from
   both checks -- it is the one package allowed to do this (T047-T052).

The module list is discovered dynamically via `pkgutil.walk_packages`
rather than hard-coded, since other agents are actively adding modules
under `civsim_harness` concurrently with this wave (per this task's
instructions) -- the test must keep working as the tree grows without
being edited.

If a module fails to import -- e.g. because another agent's file is
mid-write -- that failure is surfaced as a clear, attributable pytest
failure/error rather than masked. This file is meant to be run last
relative to concurrent work landing elsewhere in the tree.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path

import pytest

import civsim_harness

_HOST_PACKAGE = "civsim_harness.host"

# Mirrors pyproject.toml's `[tool.ruff.lint.flake8-tidy-imports.banned-api]`
# table: every banned import target there, expressed as the top-level (or
# dotted-prefix) module name that would show up in `sys.modules`.
_BANNED_MODULE_PREFIXES: tuple[str, ...] = (
    "win32api",
    "win32con",
    "win32gui",
    "win32com",
    "win32comext",
    "win32event",
    "win32file",
    "win32pipe",
    "win32process",
    "win32security",
    "win32clipboard",
    "win32service",
    "win32serviceutil",
    "win32ui",
    "pywintypes",
    "pythoncom",
    "winerror",
    "winsdk",
    "pydirectinput",
    "Quartz",
    "AppKit",
    "Xlib",
    "dbus",
)

_WALK_FAILURE_MARKER = "__walk_packages_failed__::"


def _is_host_module(name: str) -> bool:
    return name == _HOST_PACKAGE or name.startswith(_HOST_PACKAGE + ".")


def _iter_portable_modules() -> list[str]:
    """Every importable module under `civsim_harness`, excluding `host` and its
    subpackages. Discovered by walking the actual installed package tree, not a
    hard-coded list, so this keeps working as other agents add modules (T054)."""
    root = Path(civsim_harness.__file__).parent
    discovered: list[str] = []
    try:
        for module_info in pkgutil.walk_packages([str(root)], prefix="civsim_harness."):
            if _is_host_module(module_info.name):
                continue
            discovered.append(module_info.name)
    except Exception as exc:  # a package's __init__ raised while being walked to recurse
        # Turned into a single, clearly-labelled parametrize entry below
        # rather than breaking collection outright, so a mid-write package
        # under civsim_harness surfaces as one attributable failure instead
        # of taking down every other module's test with it.
        return [f"{_WALK_FAILURE_MARKER}{type(exc).__name__}: {exc}"]
    return sorted(discovered)


def _module_source_path(name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(name)
    except Exception:
        return None
    if spec is None or spec.origin is None:
        return None
    return Path(spec.origin)


_PORTABLE_MODULES = _iter_portable_modules()


@pytest.mark.parametrize("module_name", _PORTABLE_MODULES)
def test_portable_module_imports_cleanly_without_platform_libraries(module_name: str) -> None:
    if module_name.startswith(_WALK_FAILURE_MARKER):
        pytest.fail(
            "Discovering portable modules under civsim_harness failed because a "
            "package's __init__ raised while pkgutil.walk_packages imported it to "
            f"recurse: {module_name[len(_WALK_FAILURE_MARKER):]}. If this belongs to "
            "another in-progress agent's module, report it rather than editing it "
            "(per this wave's instructions)."
        )
        return

    before = set(sys.modules)
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        pytest.fail(
            f"{module_name} failed to import ({type(exc).__name__}: {exc}). If this "
            "module belongs to another in-progress agent's work, report this rather "
            "than editing it."
        )
        return

    newly_imported = set(sys.modules) - before

    def _matches_banned(name: str) -> bool:
        return any(
            name == prefix or name.startswith(prefix + ".") for prefix in _BANNED_MODULE_PREFIXES
        )

    leaked = sorted(name for name in newly_imported if _matches_banned(name))
    assert not leaked, (
        f"{module_name} pulled platform-bound module(s) {leaked} into sys.modules; "
        "OS-specific imports are only permitted under civsim_harness.host (research R19)."
    )

    source_path = _module_source_path(module_name)
    if source_path is not None and source_path.is_file():
        text = source_path.read_text(encoding="utf-8")
        assert "windll" not in text, (
            f"{module_name} references `windll` (e.g. `ctypes.windll`) directly in its "
            "source. ruff's TID251 rule does not catch this form -- OS-specific access "
            "is only permitted under civsim_harness.host (research R19)."
        )


def test_module_discovery_is_dynamic_and_excludes_host() -> None:
    """Guards the walker itself: it must discover a healthy number of real modules
    and must never include `civsim_harness.host` or any of its subpackages."""
    assert not any(name.startswith(_WALK_FAILURE_MARKER) for name in _PORTABLE_MODULES), (
        "module discovery itself failed -- see the parametrized failure above for the reason"
    )
    assert len(_PORTABLE_MODULES) >= 5, (
        f"expected several portable-core modules, discovered only {_PORTABLE_MODULES!r}; "
        "the walker may be broken rather than the tree being genuinely small"
    )
    assert not any(_is_host_module(name) for name in _PORTABLE_MODULES)


def test_host_package_itself_is_excluded_from_this_suite() -> None:
    """Sanity check on the exclusion filter used above, independent of the walk."""
    assert _is_host_module("civsim_harness.host")
    assert _is_host_module("civsim_harness.host.port")
    assert _is_host_module("civsim_harness.host.windows.adapter")
    assert not _is_host_module("civsim_harness.hostage")  # prefix must be dotted, not textual
    assert not _is_host_module("civsim_harness.store")
