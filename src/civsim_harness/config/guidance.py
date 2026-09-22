"""Guidance loading (T073, FR-021, V6).

Run-independent strategic guidance (``GUIDEBOOK.md``-shaped) is content-addressed:
the run configuration's ``guidance_set`` reference (e.g. ``GUIDEBOOK.md@a1b2c3d``,
contracts/run-configuration.md) names a source file, and *what was actually
read* is recorded with the run by its own content hash, computed here at load
time rather than trusted from the reference string.

V6 has two halves:

1. ``guidance_set``, if set, resolves to content and is recorded by hash with
   the run -- :func:`load_guidance` does the resolving and hashing.
2. Content identical across runs referencing the same hash -- guidance that
   varies per run is a contract violation, not a feature (data-model.md SS3).

**The second half is enforced by content-addressing itself, not by a check.**
``content_hash`` is a hash *of* ``content``, always recomputed from what was
just read (:func:`compute_content_hash`) rather than trusted from
``source_ref``'s ``@...`` pin -- so two runs that recorded the same
``content_hash`` recorded the same bytes, by construction. There is nothing
left for a same-process check to catch: on the only path that calls it
(:func:`load_guidance`), the hash handed to :class:`GuidanceRegistry` is
computed from the content one line earlier, so they can only ever match. An
earlier revision had :class:`GuidanceRegistry` raise on a hash collision with
different content and called that V6's "runtime assertion" -- that framing
was never honest: the guard could fire only on an actual SHA-256 collision,
which is to say never (data-model.md SS3 still describes it that way; fixing
that description is outside this module's ownership).

What makes V6's guarantee *auditable* -- across runs and process restarts,
which a same-process cache never could -- is the store recording each run's
``guidance_set_id``/``content_hash`` pair (data-model.md SS3): two runs
naming the same hash can be compared after the fact, and a real mismatch
would mean two different files were both read under the same pinned
reference, not that this loader failed to catch it in flight.
:class:`GuidanceRegistry` remains as exactly what it can honestly be: a
same-process cache saving repeat reads of identical guidance, nothing more.
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
    """A same-process cache of guidance content by its ``content_hash`` --
    convenience, not enforcement.

    V6's guarantee (content identical across runs referencing the same hash)
    is enforced by content-addressing itself: see the module docstring. This
    class used to also raise on a hash collision with different content and
    call that "the runtime assertion" -- on :func:`load_guidance`'s one call
    path, *content_hash* is always computed from *content* immediately
    before ``register`` is called, so that check could only ever fire on an
    actual SHA-256 collision. Removed rather than kept as decoration: a
    check that cannot fire on any reachable path is worse than no check, in
    that it reads as protection where there is none
    (``tests/unit/test_guidance.py`` pins that ``register`` never raises).
    """

    def __init__(self) -> None:
        self._content_by_hash: dict[str, str] = {}

    def register(self, content_hash: str, content: str) -> None:
        """Cache *content* under *content_hash* for this process's lifetime."""
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

    Raises ``PreflightError`` when the source file does not exist. It also
    caches the loaded content on *registry* by ``content_hash`` (or
    :data:`DEFAULT_REGISTRY` when none is given) -- a same-process
    convenience only; see :class:`GuidanceRegistry` and the module docstring
    for what actually enforces V6's run-independence guarantee.
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
