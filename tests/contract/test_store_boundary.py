"""Principle I, as an import-graph fact (003 FR-029; contract B1; research R11).

Nothing in the store may ever be read into the playing agent's context. The only agent-facing
path in the system is observation assembly, and it does not read the store. This test makes
that a property of the import graph rather than of reviewer diligence, the same way
``tests/unit/test_platform_neutrality.py`` does for OS libraries:

1. **Statically**: no module under the agent-facing packages names ``civsim_harness.store`` in
   an import statement.
2. **At runtime**: importing every one of those modules in a fresh interpreter never pulls a
   ``civsim_harness.store`` module into ``sys.modules`` transitively.

The package list is the one the contract names. ``run/``, ``operator/`` and ``saves/`` are
deliberately *not* on it: they are the harness's own write and control paths, and they must
import the store to persist through it.
"""

from __future__ import annotations

import ast
import json
import pkgutil
import subprocess
import sys
from pathlib import Path

import civsim_harness

AGENT_FACING_PACKAGES: tuple[str, ...] = (
    "civsim_harness.observe",
    "civsim_harness.parity",
    "civsim_harness.agent",
    "civsim_harness.capability",
    "civsim_harness.act",
    "civsim_harness.provider",
)

FORBIDDEN_PREFIX = "civsim_harness.store"


def _modules_under(package_name: str) -> list[str]:
    package = __import__(package_name, fromlist=["_"])
    names = [package_name]
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{package_name}."):
        names.append(info.name)
    return names


def _source_path(module_name: str) -> Path:
    root = Path(civsim_harness.__file__).resolve().parent.parent
    relative = Path(*module_name.split("."))
    candidate = root / relative
    if candidate.is_dir():
        return candidate / "__init__.py"
    return candidate.with_suffix(".py")


def _imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_agent_facing_packages_never_name_the_store_statically() -> None:
    offenders: dict[str, list[str]] = {}
    for package_name in AGENT_FACING_PACKAGES:
        for module_name in _modules_under(package_name):
            hits = sorted(
                name
                for name in _imports_in(_source_path(module_name))
                if name == FORBIDDEN_PREFIX or name.startswith(f"{FORBIDDEN_PREFIX}.")
            )
            if hits:
                offenders[module_name] = hits
    assert not offenders, f"agent-facing modules import the store: {offenders}"


def test_agent_facing_packages_never_load_the_store_at_runtime() -> None:
    modules = [name for package in AGENT_FACING_PACKAGES for name in _modules_under(package)]
    script = (
        "import importlib, json, sys\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        f"loaded = sorted(m for m in sys.modules if m.startswith({FORBIDDEN_PREFIX!r}))\n"
        "print(json.dumps(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=300
    )
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert loaded == [], f"importing agent-facing packages loaded the store: {loaded}"
