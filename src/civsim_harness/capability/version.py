"""Catalog content hashing (T036).

FR-022 / SC-007 require the catalog version and its computed content hash to
be recorded on every run, so two runs claiming the same ``catalogs/VERSION``
string can still be told apart if the files backing it changed underneath
that string (a version bump is a human action; a hash catches the case where
it was forgotten). :func:`compute_content_hash` folds in both the bytes of
every catalog file *and* the resulting ``declaration_ids`` set, per T036's
brief -- the declaration set is, in the ordinary case, fully determined by
the file bytes already hashed, but hashing it explicitly as well means a
change in *how* the loader interprets those bytes into declarations (a
loader bug or a future format change) also changes the recorded hash, not
just a change in the bytes themselves.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path


def compute_content_hash(
    *,
    root: Path,
    files: Iterable[Path],
    declaration_ids: Iterable[str],
) -> str:
    """A stable sha256 hex digest over *files* (relative to *root*) and *declaration_ids*.

    Both inputs are sorted before hashing so the digest depends only on
    content, never on filesystem iteration order (not guaranteed stable
    across platforms or directory listings) or on dict/set ordering.
    """
    digest = hashlib.sha256()

    resolved_root = root.resolve()

    def _relpath(path: Path) -> str:
        return path.resolve().relative_to(resolved_root).as_posix()

    sorted_files = sorted(files, key=_relpath)
    for path in sorted_files:
        relative = _relpath(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")

    for declaration_id in sorted(declaration_ids):
        digest.update(declaration_id.encode("utf-8"))
        digest.update(b"\0")

    return digest.hexdigest()
