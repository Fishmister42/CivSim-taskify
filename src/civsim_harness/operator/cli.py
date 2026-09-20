"""Typer CLI entry point for the civsim operator surface.

This is the ``civsim`` console script (see pyproject.toml ``[project.scripts]``).
It is a stub: lifecycle commands (``prepare``, ``run``, ``pause``, ``resume``,
``abort``, ``doctor``, ``audit``, ``seedset``) are added by later phases against
the port defined in contracts/operator-surface.md. The CLI never presents turn
records, decisions, metrics, or captures (FR-053, Principle VI) -- that surface
belongs to deliverable 1.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="civsim",
    help="Operator CLI for the Civilization-Playing Harness (lifecycle and diagnostics only).",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the harness package version."""
    from civsim_harness import __version__

    typer.echo(f"civsim-harness {__version__}")


if __name__ == "__main__":
    app()
