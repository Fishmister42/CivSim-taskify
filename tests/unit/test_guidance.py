"""Unit tests for guidance loading (T073, FR-021, V6).

This suite exists to pin one specific correction: `GuidanceRegistry.register`
used to raise on a hash collision with different content, framed as V6's
"runtime assertion" that guidance is run-independent. On `load_guidance`'s one
call path, `content_hash` is always computed from `content` immediately
before `register` is called (`config/guidance.py`), so that check could only
ever fire on an actual SHA-256 collision -- never, in practice. The raise was
removed; `register` is now a plain same-process cache. See the module
docstring in `config/guidance.py` for the full account of what actually
enforces V6 (content-addressing itself, plus the store's recorded
`content_hash` per run).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from civsim_harness.config.guidance import GuidanceRegistry, compute_content_hash, load_guidance
from civsim_harness.errors import PreflightError


def test_load_guidance_returns_a_content_addressed_set(tmp_path: Path) -> None:
    guidebook = tmp_path / "GUIDEBOOK.md"
    guidebook.write_text("Expand early. Never declare war on turn one.", encoding="utf-8")

    guidance_set = load_guidance(
        "GUIDEBOOK.md@deadbeef", root=tmp_path, registry=GuidanceRegistry()
    )

    assert guidance_set.source_ref == "GUIDEBOOK.md@deadbeef"
    assert guidance_set.content == guidebook.read_text(encoding="utf-8")
    assert guidance_set.content_hash == compute_content_hash(guidance_set.content)
    # The `@...` pin is carried through verbatim, never used to derive the hash.
    assert guidance_set.guidance_set_id == guidance_set.content_hash


def test_load_guidance_missing_file_raises_preflight_error(tmp_path: Path) -> None:
    with pytest.raises(PreflightError):
        load_guidance("GUIDEBOOK.md@deadbeef", root=tmp_path, registry=GuidanceRegistry())


def test_registering_the_same_hash_with_different_content_does_not_raise() -> None:
    """The behaviour this whole suite exists to pin: `register` is a cache,
    not an assertion. A caller handing it a *content_hash* that does not
    actually match *content* -- the only way to exercise a "collision" at
    all, since `load_guidance` itself can never produce one -- is not
    refused; the second write simply wins, exactly like any other cache.
    """
    registry = GuidanceRegistry()
    registry.register("same-hash", "guidance A")
    registry.register("same-hash", "guidance B")  # must not raise

    assert registry.content_for("same-hash") == "guidance B"


def test_load_guidance_on_its_one_call_path_can_never_hit_a_hash_mismatch(
    tmp_path: Path,
) -> None:
    """Pins the actual reason the old raise was dead code: `load_guidance`
    always computes `content_hash` from the content it just read, one line
    before registering it, so two different `load_guidance` calls that
    resolve to files with different content also resolve to different
    hashes (barring an actual SHA-256 collision) and never collide in the
    registry at all.
    """
    registry = GuidanceRegistry()
    (tmp_path / "a.md").write_text("guidance A", encoding="utf-8")
    (tmp_path / "b.md").write_text("guidance B", encoding="utf-8")

    set_a = load_guidance("a.md", root=tmp_path, registry=registry)
    set_b = load_guidance("b.md", root=tmp_path, registry=registry)

    assert set_a.content_hash != set_b.content_hash
    assert registry.content_for(set_a.content_hash) == "guidance A"
    assert registry.content_for(set_b.content_hash) == "guidance B"


def test_load_guidance_reloading_identical_content_is_a_cache_hit_not_a_conflict(
    tmp_path: Path,
) -> None:
    registry = GuidanceRegistry()
    guidebook = tmp_path / "GUIDEBOOK.md"
    guidebook.write_text("Expand early.", encoding="utf-8")

    first = load_guidance("GUIDEBOOK.md", root=tmp_path, registry=registry)
    second = load_guidance("GUIDEBOOK.md", root=tmp_path, registry=registry)  # must not raise

    assert first.content_hash == second.content_hash
    assert registry.content_for(first.content_hash) == "Expand early."
