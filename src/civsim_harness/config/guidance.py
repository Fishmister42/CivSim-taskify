"""Guidance loading (T073, FR-021, V6).

Run-independent strategic guidance (``GUIDEBOOK.md``-shaped) is content-addressed:
the run configuration's ``guidance_set`` reference (e.g. ``GUIDEBOOK.md@a1b2c3d``,
contracts/run-configuration.md) names a source file, and *what was actually
read* is recorded with the run by its own content hash, computed here at load
time rather than trusted from the reference string.

V6 has two halves:

1. ``guidance_set``, if set, resolves to content and is recorded by hash with
   the run -- :func:`load_guidance` does the resolving and hashing.
2. The runtime assertion that content is identical across runs referencing the
   same hash -- guidance that varies per run is a contract violation, not a
   feature (data-model.md SS3). :class:`GuidanceRegistry` is what enforces
   *that* half: a hash collision with different content raises immediately
   rather than silently keeping whichever content loaded first.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from civsim_harness.errors import PreflightError
from civsim_harness.models.common import GuidanceSetId
from civsim_harness.models.config import GuidanceSet


def compute_content_hash(content: str) -> str:
    """A stable sha256 hex digest over guidance *content* (V6)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_guidance_content(path: Path) -> str:
    if not path.is_file():
        raise PreflightError(
            "guidance_set source file does not exist", detail={"path": str(path)}
        )
    return path.read_text(encoding="utf-8")


class GuidanceRegistry:
    """Enforces V6's runtime assertion: content identical across runs
    referencing the same hash.

    A process-lifetime cache is enough for this feature's own loader to
    self-check; it is not a substitute for the store recording each run's
    ``guidance_set_id``/``content_hash`` pair, which is what makes the
    assertion auditable across process restarts too.
    """

    def __init__(self) -> None:
        self._content_by_hash: dict[str, str] = {}

    def register(self, content_hash: str, content: str) -> None:
        """Record *content* under *content_hash*, or raise if a different
        content was already registered under the same hash (V6, data-model.md
        SS3: "guidance that varies per run is a contract violation").
        """
        existing = self._content_by_hash.get(content_hash)
        if existing is not None and existing != content:
            raise PreflightError(
                "guidance content differs from a previously loaded run referencing "
                "the same content_hash -- guidance must be content-addressed and "
                "run-independent (FR-021, V6)",
                detail={"content_hash": content_hash},
            )
        self._content_by_hash[content_hash] = content

    def content_for(self, content_hash: str) -> str | None:
        return self._content_by_hash.get(content_hash)


#: Process-wide default registry. A caller that wants isolation (tests, or a
#: multi-tenant host) constructs and passes its own ``GuidanceRegistry``
#: instead -- :func:`load_guidance` never mutates module state other than
#: through whichever registry it is given.
DEFAULT_REGISTRY = GuidanceRegistry()


def load_guidance(
    source_ref: str,
    *,
    root: Path,
    registry: GuidanceRegistry | None = None,
    guidance_set_id: GuidanceSetId | None = None,
) -> GuidanceSet:
    """Load guidance referenced by *source_ref* (e.g. ``"GUIDEBOOK.md@a1b2c3d"``)
    relative to *root*, and return a :class:`GuidanceSet` carrying its
    content-addressed hash.

    The ``@...`` suffix (a commit or tag pin) is provenance carried through
    verbatim as ``source_ref``; the file is always read as it exists on disk
    under *root* right now -- resolving a specific historical revision of the
    file is out of scope for this loader. ``content_hash`` is always computed
    from what was actually read, which is what V6 requires: the recorded hash
    describes the content, not the reference string.

    Raises ``PreflightError`` when the source file does not exist, or when the
    computed hash collides with different content already registered for
    *registry* (V6's runtime assertion).
    """
    active_registry = registry if registry is not None else DEFAULT_REGISTRY
    file_part = source_ref.split("@", 1)[0]
    path = (root / file_part).resolve()
    content = _read_guidance_content(path)
    content_hash = compute_content_hash(content)
    active_registry.register(content_hash, content)

    return GuidanceSet(
        guidance_set_id=(
            guidance_set_id if guidance_set_id is not None else GuidanceSetId(content_hash)
        ),
        content_hash=content_hash,
        content=content,
        source_ref=source_ref,
    )
