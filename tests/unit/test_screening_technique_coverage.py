"""The structural guard: every shipped reject category must have a technique that addresses it.

The content gate is two halves that were never checked against each other. One half is
``catalogs/screening_profiles.yaml``, which *names* what must be rejected per platform. The other
is ``parity/screening.py``'s ``DefaultContentDetector``, which *implements* three techniques. A
category named in the catalog but addressed by no technique produced no match, and "no match" was
read as "clean" -- so the gate's strictest-looking profiles were partly decorative. Eight of the
ten shipped reject ids were in that state on every platform whenever no text evidence was
gathered, which in production was always.

Fixing the gate to withhold such a category (the ``_check_content`` coverage check) stops the
frame escaping. It does not stop the *gap* recurring: a new reject id added to the catalog
tomorrow, or a technique deleted from the detector, would silently push more categories into the
uncovered set and simply withhold more, which reads as a stricter gate rather than a broken one.
This file is what makes that loud instead.

Both halves are derived, never transcribed. The categories come from
:func:`~civsim_harness.parity.screening.load_screening_profiles` reading the real catalog file;
the coverage comes from :func:`~civsim_harness.parity.screening.techniques_for_category`, the
published mapping the detector itself partitions on. Nothing here restates a list that lives
somewhere else, so neither half can drift out from under the other -- and
:func:`test_the_coverage_assertion_can_actually_fail` is the negative control proving the
assertion is capable of going red at all.

The per-platform parametrisation deliberately covers **windows and macos as well as linux**. The
dead technique is platform-independent: every category whose only technique is declared-text
matching was unscreened on all three, so the capture-hygiene passes recorded for Windows and
macOS rest on the same silence the Linux one did.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from civsim_harness.host.detect import OperatingSystem
from civsim_harness.parity.screening import (
    DefaultContentDetector,
    ScreeningProfiles,
    ScreeningTechnique,
    load_screening_profiles,
    techniques_for_category,
    unaddressed_reject_categories,
)

ALL_TECHNIQUES = frozenset(ScreeningTechnique)


@pytest.fixture(scope="module")
def shipped() -> ScreeningProfiles:
    """The real ``catalogs/screening_profiles.yaml``, not a fixture copy."""
    return load_screening_profiles()


def _shipped_profile_names() -> list[str]:
    """Every profile the repository actually ships, read from the catalog at collection time."""
    return sorted(load_screening_profiles().profiles)


@pytest.mark.parametrize("profile_name", _shipped_profile_names())
def test_every_reject_category_in_every_shipped_profile_is_addressable(
    profile_name: str, shipped: ScreeningProfiles
) -> None:
    """No shipped profile may name a category that no technique can decide, on any platform.

    This is the invariant the gate's strictness claim rests on. A profile listing a category
    nothing implements is not strict -- it is a promise with no mechanism behind it, and until
    the coverage check landed it was a promise that also passed the frame.
    """
    profile = shipped.profiles[profile_name]
    uncovered = unaddressed_reject_categories(
        profile.reject, available_techniques=ALL_TECHNIQUES
    )

    assert not uncovered, (
        f"profile {profile_name!r} names reject categories no technique addresses: "
        f"{sorted(uncovered)}. Either implement a technique for them in "
        f"DefaultContentDetector (and register its trigger tokens), or remove them from "
        f"catalogs/screening_profiles.yaml -- do not leave the catalog claiming a check that "
        f"does not exist."
    )


def test_every_defined_reject_id_is_addressable_even_if_no_profile_uses_it_yet(
    shipped: ScreeningProfiles,
) -> None:
    """``reject_definitions`` is the vocabulary profiles draw from; an unimplementable word in it
    becomes an unimplementable profile the moment someone uses it."""
    uncovered = unaddressed_reject_categories(
        frozenset(shipped.reject_definitions), available_techniques=ALL_TECHNIQUES
    )

    assert not uncovered, f"reject_definitions defines unaddressable ids: {sorted(uncovered)}"


def test_the_catalog_ships_a_profile_for_every_operating_system_the_repo_supports(
    shipped: ScreeningProfiles,
) -> None:
    """Derived both sides: the platform list comes from ``OperatingSystem``, not from a literal.

    Without this, the parametrisation above could quietly shrink -- deleting the ``macos``
    profile would remove its own coverage test rather than fail one, and resolution would fall
    back to ``default`` with nobody noticing the platform had lost its dedicated entry.
    """
    missing = {os.value for os in OperatingSystem} - set(shipped.profiles)

    assert not missing, f"no screening profile ships for supported platform(s): {sorted(missing)}"


@pytest.mark.parametrize("profile_name", _shipped_profile_names())
def test_without_text_evidence_every_shipped_profile_is_left_partly_unscreened(
    profile_name: str, shipped: ScreeningProfiles
) -> None:
    """The live defect, asserted per platform: no text evidence means an unscreened profile.

    Windows and macOS are included deliberately. Their recorded capture-hygiene passes were
    obtained through the same detector with the same missing evidence, so whatever those runs
    demonstrated, it was not that ``firetuner_window`` or ``developer_console`` were screened --
    nothing examined them there either.

    When the evidence *is* gathered, coverage is complete: that is the second assertion, and it
    is what makes the first one a statement about missing plumbing rather than about a
    permanently incapable detector.
    """
    detector = DefaultContentDetector()
    reject = shipped.profiles[profile_name].reject

    without = detector.addressable_categories(reject, text_evidence_available=False)
    assert without < reject, (
        f"profile {profile_name!r} is fully addressable with no text evidence at all, which "
        "would mean the declared-text technique is no longer load-bearing -- check whether this "
        "test, or the detector, has stopped describing reality"
    )
    assert "firetuner_window" in (reject - without)

    with_evidence = detector.addressable_categories(reject, text_evidence_available=True)
    assert with_evidence == reject


def test_the_detectors_declared_coverage_is_the_coverage_it_partitions_on(
    shipped: ScreeningProfiles,
) -> None:
    """``addressable_categories`` and ``detect`` must not be able to disagree.

    Both are defined in terms of :func:`techniques_for_category`. If one were ever reimplemented
    with its own inline token rules -- which is how the original three ``if`` blocks came to be
    the only record of the mapping -- this catches the divergence.
    """
    detector = DefaultContentDetector()
    reject = shipped.profiles["default"].reject

    for technique in ScreeningTechnique:
        by_mapping = {c for c in reject if technique in techniques_for_category(c)}
        assert by_mapping, f"no shipped category is addressed by {technique.value}"

    assert detector.addressable_categories(reject, text_evidence_available=True) == frozenset(
        c for c in reject if techniques_for_category(c)
    )


# --------------------------------------------------------------------------
# Negative control: the assertion above must be capable of failing
# --------------------------------------------------------------------------

_UNCOVERABLE_CATALOG = """
schema_version: 1
reject_definitions:
  firetuner_window: FireTuner.
  debug_overlay: A debug overlay.
  iridescent_shimmer: >
    A category deliberately named from words no technique in DefaultContentDetector looks for --
    not "border", not "overlay"/"debug", and (in the no-text-evidence case) nothing a text
    source would be asked about either.
universal_reject: [firetuner_window]
profiles:
  linux:
    description: linux
    reject: [firetuner_window, debug_overlay, iridescent_shimmer]
  default:
    description: default
    reject: [firetuner_window, debug_overlay, iridescent_shimmer]
"""


def test_the_coverage_assertion_can_actually_fail(tmp_path: Path) -> None:
    """A positive control alone proves nothing; this is the negative one.

    A profile carrying a category built from words no *image* technique names is exactly the
    shape the real catalog must never have, and the check above must report it. Run against a
    synthetic catalog file so the repository's own data is never edited to prove a test works.

    Note what makes ``iridescent_shimmer`` uncovered and what does not: with text evidence
    available it *is* addressable (the declared-text technique applies to any id), which is the
    honest answer -- something could look for those words. With no text evidence it is addressed
    by nothing at all, and that is the state the production gate is in.
    """
    catalog = tmp_path / "screening_profiles.yaml"
    catalog.write_text(_UNCOVERABLE_CATALOG, encoding="utf-8")
    loaded = load_screening_profiles(catalog)
    reject = loaded.profiles["linux"].reject

    image_only = ALL_TECHNIQUES - {ScreeningTechnique.DECLARED_TEXT}
    uncovered = unaddressed_reject_categories(reject, available_techniques=image_only)

    assert uncovered == {"firetuner_window", "iridescent_shimmer"}
    assert not unaddressed_reject_categories(reject, available_techniques=ALL_TECHNIQUES)

    # And the same gap, reported through the detector's own coverage declaration.
    detector = DefaultContentDetector()
    assert detector.addressable_categories(reject, text_evidence_available=False) == {
        "debug_overlay"
    }


def test_a_category_with_no_tokens_at_all_is_addressed_by_nothing() -> None:
    """The degenerate id. It cannot be matched by any rule, so it must never read as covered."""
    assert techniques_for_category("") == frozenset()
    assert techniques_for_category("___") == frozenset()
    assert unaddressed_reject_categories(
        frozenset({"___"}), available_techniques=ALL_TECHNIQUES
    ) == {"___"}
