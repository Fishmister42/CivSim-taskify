"""The structural guard: every shipped reject category must be one something can actually decide.

**T299 changed the verb in that sentence from "address" to "decide", and that is the whole point
of this revision.** The guard used to assert *addressability*: does some technique claim this
category? It passed, on every profile, while three shipped categories could not be matched by
anything -- because the declared-text technique derived its keywords from the category id, and
``firetuner_window`` needs the word "window", ``harness_owned_ui`` needs "owned" and "ui", and
``linux_panel`` needs "linux", none of which a window title supplies. T264's fail-closed rule
therefore never fired for them and this guard never went red: the frame was delivered and the
record said it had been screened. A guard satisfied by a vocabulary mismatch is a guard that
cannot fail in the way that matters, which made it worse than the hole it replaced.

The guard now asserts *matchability*, and it is red where reality is red. A category counts only
if (a) the catalog declares a keyword vocabulary that a declared witness string demonstrably
produces under the evidence source's own tokeniser -- proved at load, not here -- or (b) an image
technique looks for what its id tokens name, or (c) the profile's declared capture scope
structurally rules the category out, with the measurement recorded. Windows, macOS and the
``default`` profile satisfy none of those for ``firetuner_window`` and ``harness_owned_ui``, so
their parametrisations are ``xfail(strict=True)``: the release blocker is executable, and closing
it requires deleting an entry from ``_UNCOVERED_PROFILES`` rather than quietly turning green.

---


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

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from civsim_harness.capability.loader import load_catalog
from civsim_harness.errors import CatalogError
from civsim_harness.host.detect import OperatingSystem, detect_host_info
from civsim_harness.host.port import CaptureFrame, WindowRect, window_text_tokens
from civsim_harness.models.catalog import (
    CameraMode,
    CameraRequirements,
    DeclarationKind,
    ParityDeclaration,
    ScreeningProfileDeclaration,
)
from civsim_harness.parity.screening import (
    DefaultContentDetector,
    RejectOrigin,
    ScreeningProfiles,
    ScreeningTechnique,
    load_screening_profiles,
    resolve_screening_profile,
    techniques_for_category,
    unaddressed_reject_categories,
)

ALL_TECHNIQUES = frozenset(ScreeningTechnique)
REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"


def _flat_png_frame() -> CaptureFrame:
    """A decodable, utterly featureless frame: no image technique can fire on it.

    The text-technique assertions in this file are about the vocabulary, not about pixels, and a
    frame that tripped the border or corner detector would make them pass for the wrong reason.
    """
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (90, 90, 90)).save(buffer, format="PNG")
    return CaptureFrame(
        width=64,
        height=64,
        rect=WindowRect(0, 0, 64, 64),
        image_bytes=buffer.getvalue(),
        image_format="PNG",
    )


_ONE_PIXEL_PNG_FRAME = _flat_png_frame()


@pytest.fixture(scope="module")
def shipped() -> ScreeningProfiles:
    """The real ``catalogs/screening_profiles.yaml``, not a fixture copy."""
    return load_screening_profiles()


def _shipped_profile_names() -> list[str]:
    """Every profile the repository actually ships, read from the catalog at collection time."""
    return sorted(load_screening_profiles().profiles)


#: Profiles whose reject set T299 leaves genuinely uncovered, named here rather than derived, so
#: the list can only shrink deliberately. Deriving it from the catalog would make the guard below
#: assert that the catalog agrees with itself -- which is exactly the shape of the defect this
#: task exists to remove. Each of these platforms has NO structural protection (its
#: ``capture_scope`` is ``unproven``) AND no matchable vocabulary for ``firetuner_window`` and
#: ``harness_owned_ui``, so nothing screens them and nothing can. Landing a vocabulary or a
#: capture-scope spike for one makes its ``xfail`` an XPASS, which is strict and therefore red:
#: whoever closes the gap is forced to delete its entry here.
_UNCOVERED_PROFILES: frozenset[str] = frozenset({"windows", "macos", "default"})


def _coverage_params() -> list[Any]:
    return [
        pytest.param(
            name,
            marks=(
                [
                    pytest.mark.xfail(
                        strict=True,
                        reason=(
                            f"T299 release blocker: the {name!r} profile names reject categories "
                            "with no matchable vocabulary and no structural exclusion. Nothing "
                            "screens them; the gate withholds rather than certifying."
                        ),
                    )
                ]
                if name in _UNCOVERED_PROFILES
                else []
            ),
        )
        for name in _shipped_profile_names()
    ]


def _uncovered_in(profile_name: str, shipped: ScreeningProfiles) -> frozenset[str]:
    """What nothing can decide for *profile_name*: not addressed, and not structurally excluded.

    The same two-part sum :func:`~civsim_harness.parity.screening._check_content` computes, so
    the guard cannot pass while the runtime withholds (or the reverse).
    """
    profile = shipped.profiles[profile_name]
    unaddressed = unaddressed_reject_categories(
        profile.reject,
        available_techniques=ALL_TECHNIQUES,
        text_vocabulary=shipped.text_vocabulary,
    )
    return unaddressed - shipped.structurally_excluded(profile)


@pytest.mark.parametrize("profile_name", _coverage_params())
def test_every_reject_category_in_every_shipped_profile_is_matchable(
    profile_name: str, shipped: ScreeningProfiles
) -> None:
    """No shipped profile may name a category nothing can decide -- **matchable**, not merely
    addressable.

    This is the invariant the gate's strictness claim rests on, and until T299 it was asserted one
    word too weakly. ``unaddressed_reject_categories`` used to count the declared-text technique
    as addressing every category, because its rule ("are this id's own tokens all in the
    evidence?") is well defined for any id. Well defined is not satisfiable: no window title
    supplies ``"window"``, ``"owned"``, ``"ui"`` or ``"linux"``, so ``firetuner_window``,
    ``harness_owned_ui`` and ``linux_panel`` were addressed by a rule that could never fire, this
    assertion passed, and the frame went out recorded as screened. A guard satisfied by a
    vocabulary mismatch is a guard that cannot fail in the way that matters.

    Now a category counts only if the catalog declares a vocabulary a witness string can actually
    produce (proved at load), an image technique looks for what its tokens name, or the profile's
    declared capture scope structurally rules the category out. The ``xfail``\\ s above are the
    honest remainder: on Windows and macOS nothing screens ``firetuner_window``, nothing can, and
    this guard now says so instead of passing.
    """
    uncovered = _uncovered_in(profile_name, shipped)

    assert not uncovered, (
        f"profile {profile_name!r} (capture_scope "
        f"{shipped.profiles[profile_name].capture_scope.name!r}) names reject categories nothing "
        f"can decide: {sorted(uncovered)}. Three ways out, in order of preference: declare a "
        f"keyword vocabulary with a witness the evidence source can really produce "
        f"(catalogs/screening_profiles.yaml, reject_definitions.<id>.text_keywords); implement an "
        f"image technique and register its trigger tokens; or establish the platform's capture "
        f"scope with a spike so the category is structurally excluded. Do NOT invent keywords to "
        f"turn this green -- a vocabulary that matches nothing is the defect this guard exists to "
        f"catch."
    )


def test_every_defined_reject_id_either_has_a_vocabulary_or_says_why_it_has_none(
    shipped: ScreeningProfiles,
) -> None:
    """The honesty requirement, asserted over the whole vocabulary rather than the shipped subset.

    T299's rule is not "every category must be matchable" -- that would be satisfiable by
    invention, which is the failure mode. It is "every category must either be matchable or state,
    in the catalog, why no vocabulary could be established and what evidence would settle it".
    Silence is what made ``firetuner_window`` look addressed.
    """
    for category_id, definition in sorted(shipped.reject_definitions.items()):
        if definition.text_matchable:
            assert definition.text_basis, (
                f"{category_id!r} declares keywords with no recorded provenance; a keyword list "
                "with no basis is a guess"
            )
            assert definition.witnesses
            continue
        assert definition.text_unmatchable, (
            f"{category_id!r} has no declared keyword vocabulary and does not say why. That is "
            "the pre-T299 state exactly: a category that matches nothing while the coverage map "
            "reports it as addressed."
        )
        assert definition.text_settled_by, (
            f"{category_id!r} states it has no vocabulary but not what evidence would establish "
            "one, which turns an open question into a permanent one"
        )


def test_exactly_one_shipped_category_has_a_vocabulary_the_evidence_source_can_produce(
    shipped: ScreeningProfiles,
) -> None:
    """The measured state on 2026-09-22, pinned so it cannot quietly grow by invention.

    One category, ``developer_console``, has keywords a window title demonstrably supplies. Nine
    do not. That ratio is the finding, and it is pinned deliberately: a future commit that adds a
    vocabulary has to come through this test, where the reviewer is asked for the witness and the
    basis rather than for a green guard.
    """
    matchable = {c for c, d in shipped.reject_definitions.items() if d.text_matchable}

    assert matchable == {"developer_console"}, (
        f"the set of text-matchable reject categories changed to {sorted(matchable)}. If a "
        "vocabulary was genuinely established from a primary source, update this test and say "
        "where the witness came from. If it was invented to make a guard pass, this is the test "
        "that was supposed to stop you."
    )


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
    demonstrated, it was not that ``developer_console`` was screened -- nothing examined it there
    either.

    T299 changed which category this test can name. It used to name ``firetuner_window``, on the
    belief that the declared-text technique was its only one and therefore load-bearing for it;
    that belief was the defect, since the technique could not match it under any evidence. The
    category that genuinely turns on text evidence is ``developer_console`` -- the one with a
    vocabulary a window title can supply -- and naming it is what keeps this test a statement
    about missing plumbing rather than about a mismatch.
    """
    detector = DefaultContentDetector()
    reject = shipped.profiles[profile_name].reject
    vocabulary = shipped.text_vocabulary

    without = detector.addressable_categories(
        reject, text_evidence_available=False, text_vocabulary=vocabulary
    )
    assert without < reject, (
        f"profile {profile_name!r} is fully addressable with no text evidence at all, which "
        "would mean the declared-text technique is no longer load-bearing -- check whether this "
        "test, or the detector, has stopped describing reality"
    )
    assert "developer_console" in (reject - without)

    with_evidence = detector.addressable_categories(
        reject, text_evidence_available=True, text_vocabulary=vocabulary
    )
    assert "developer_console" in with_evidence
    # And gathering evidence does NOT complete coverage any more, on any profile: the categories
    # with no establishable vocabulary stay unaddressed however much the desktop is enumerated.
    # That was the sentence this test used to end on (``with_evidence == reject``), and it was
    # true only because an unmatchable rule counted as a technique.
    assert "firetuner_window" not in with_evidence


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
    vocabulary = shipped.text_vocabulary

    for technique in ScreeningTechnique:
        by_mapping = {
            c for c in reject if technique in techniques_for_category(c, text_vocabulary=vocabulary)
        }
        assert by_mapping, f"no shipped category is addressed by {technique.value}"

    assert detector.addressable_categories(
        reject, text_evidence_available=True, text_vocabulary=vocabulary
    ) == frozenset(c for c in reject if techniques_for_category(c, text_vocabulary=vocabulary))


def test_a_vocabulary_is_never_derived_from_the_category_name(
    shipped: ScreeningProfiles,
) -> None:
    """The root cause, asserted directly: the id's own tokens are not evidence of anything.

    ``firetuner_window`` fired on nothing because its keyword list *was* its name. With the
    vocabulary supplied as data and the shipped catalog declaring none for it, no token set --
    not even its own name tokens, the one set the old rule was guaranteed to match -- may make it
    addressable or make it fire.
    """
    detector = DefaultContentDetector()
    vocabulary = shipped.text_vocabulary
    reject = shipped.profiles["default"].reject

    assert ScreeningTechnique.DECLARED_TEXT not in techniques_for_category(
        "firetuner_window", text_vocabulary=vocabulary
    )

    name_tokens = frozenset({"firetuner", "window", "harness", "owned", "ui", "linux", "panel"})
    matched = detector.detect(
        _ONE_PIXEL_PNG_FRAME,
        reject_categories=reject,
        detected_text_tokens=name_tokens,
        text_vocabulary=vocabulary,
    )

    assert "firetuner_window" not in matched
    assert "harness_owned_ui" not in matched
    assert "linux_panel" not in matched


def test_the_one_matchable_vocabulary_actually_fires(shipped: ScreeningProfiles) -> None:
    """The positive control for the above: ``developer_console`` still behaves exactly as before.

    T299 must not be a fix that works by turning the technique off. The category whose keywords a
    window title can supply matches when they are present, and a single stray word is still not
    enough -- the group's words must ALL be there, which is the property that stopped the
    technique producing trivial false positives in the first place.
    """
    detector = DefaultContentDetector()
    vocabulary = shipped.text_vocabulary
    reject = shipped.profiles["linux"].reject

    fired = detector.detect(
        _ONE_PIXEL_PNG_FRAME,
        reject_categories=reject,
        detected_text_tokens=window_text_tokens("Developer Console"),
        text_vocabulary=vocabulary,
    )
    assert "developer_console" in fired

    partial = detector.detect(
        _ONE_PIXEL_PNG_FRAME,
        reject_categories=reject,
        detected_text_tokens=frozenset({"console"}),
        text_vocabulary=vocabulary,
    )
    assert "developer_console" not in partial


# --------------------------------------------------------------------------
# T299: structural exclusion is declared, bounded, and never a blanket pass
# --------------------------------------------------------------------------


def test_only_the_platform_with_a_measured_capture_scope_excludes_anything(
    shipped: ScreeningProfiles,
) -> None:
    """The hypervisor ruling, as an assertion rather than as prose in a markdown file.

    Linux delivery is open because the X11 path reads the game window's own backing store, so
    another application's window cannot be in the frame -- a structural fact, not a gate result.
    Windows and macOS have no equivalent spike, so they exclude nothing and their uncovered
    categories stay uncovered. If someone ever declares a backing-store scope for them without
    the evidence, this is where it shows up as a change rather than as a quietly greener guard.
    """
    excluding = {
        name for name, profile in shipped.profiles.items() if shipped.structurally_excluded(profile)
    }

    assert excluding == {"linux"}, (
        f"profiles claiming a structural exclusion: {sorted(excluding)}. Only 'linux' has a "
        "measured capture scope (spikes/r6-capture-hygiene-linux.md, corroborated by the 188-"
        "frame retro-audit). A structural exemption without a spike behind it is a stronger "
        "claim than any technique makes and needs stronger evidence, not weaker."
    )
    assert shipped.profiles["linux"].capture_scope_basis


def test_in_frame_chrome_is_never_structurally_excluded(shipped: ScreeningProfiles) -> None:
    """The bound on the exclusion: it rules out other windows, not other content.

    Every capture scope in this catalog still returns the client's own pixels, so anything drawn
    INSIDE that surface -- the game's in-engine console, a compositing third party's overlay, the
    capture API's own recording border -- is exactly as possible on Linux as anywhere else. An
    exclusion that swallowed those would be the fail-open hole this whole lane has been closing,
    reopened under a better name.
    """
    for name, profile in shipped.profiles.items():
        for category in shipped.structurally_excluded(profile):
            assert shipped.reject_definitions[category].origin is RejectOrigin.SEPARATE_WINDOW, (
                f"profile {name!r} structurally excludes {category!r}, which the catalog declares "
                "as in-frame chrome; no capture path in this file can rule that out"
            )

    linux = shipped.profiles["linux"]
    excluded = shipped.structurally_excluded(linux)
    assert "debug_overlay" not in excluded
    assert "developer_console" not in excluded


def test_the_linux_profile_is_covered_without_citing_firetuner_as_screened(
    shipped: ScreeningProfiles,
) -> None:
    """The ruling's other half: Linux is covered, and NOT because a technique screens FireTuner.

    Both halves matter and they are easy to conflate, which is how the record came to say the
    frame had been screened. ``firetuner_window`` is covered on Linux by structure and by nothing
    else; if a future change ever makes a technique address it, this test fails and whoever made
    that change has to produce the evidence for it.
    """
    detector = DefaultContentDetector()
    linux = shipped.profiles["linux"]

    assert _uncovered_in("linux", shipped) == frozenset()

    by_technique = detector.addressable_categories(
        linux.reject, text_evidence_available=True, text_vocabulary=shipped.text_vocabulary
    )
    assert "firetuner_window" not in by_technique, (
        "a technique now claims to address firetuner_window. The 2026-09-22 ruling is that this "
        "category is undetectable by the current technique set and must not be cited as screened "
        "anywhere -- so this needs the evidence, not a passing test."
    )
    assert "firetuner_window" in shipped.structurally_excluded(linux)


# --------------------------------------------------------------------------
# T292: the profile that actually resolves must be the running host's own
# --------------------------------------------------------------------------
#
# A second way for this gate to be decorative, and the one the retro-audit caught in the record:
# the techniques were fine and the categories were covered, but the *profile* being screened
# against was the wrong one. 34 content-gate withholds across ``run-227998759f75`` (19) and
# ``run-fd9c08a1df1d`` (15) -- both recorded ``os=linux, session_type=x11`` -- named
# ``windows_capture_border``, a category describing a border the Windows.Graphics.Capture API
# draws and an XComposite frame cannot contain. A Windows recording-border test was evaluating
# Linux frames, firing on the game's own dark UI edging, and the withholds it produced were filed
# as safety. This is not the "mechanism never reached" shape the rest of this file guards: the
# mechanism ran, reached the wrong thing, and was counted as if it had worked.
#
# The cause was the declaration. The views said ``screening_profile: default``, which means "the
# strictest union profile regardless of host" -- the union of every platform's chrome, including
# every platform that is not this one. 98c71bb changed it to ``platform``, which was not a key in
# ``screening_profiles.yaml`` either; it only worked because the resolver treated every
# non-``default`` string as "resolve by host". The assertions below are about the resolved
# *outcome* rather than the declared string, so neither a typo nor a future catalog edit can put
# a profile in front of a frame that does not belong to the host that captured it.


def _shipped_views() -> list[ParityDeclaration]:
    """Every ``kind: view`` declaration the repository actually ships, read at collection time."""
    catalog = load_catalog(CATALOG_ROOT)
    return [d for d in catalog.declarations.values() if d.kind is DeclarationKind.VIEW]


def _shipped_view_ids() -> list[str]:
    return sorted(str(view.declaration_id) for view in _shipped_views())


_PLATFORM_KEYS = frozenset(os.value for os in OperatingSystem)


@pytest.mark.parametrize("view_id", _shipped_view_ids())
@pytest.mark.parametrize("platform", sorted(_PLATFORM_KEYS))
def test_a_shipped_view_resolves_to_the_profile_of_the_host_it_is_screened_on(
    view_id: str, platform: str, shipped: ScreeningProfiles
) -> None:
    """The invariant the 34 withholds violated, asserted as data on every supported platform.

    Both sides are derived: the declared value comes from the real ``catalogs/observations/
    views.yaml``, the expected profile name from ``OperatingSystem``. A Linux host resolving
    anything but the Linux profile fails here -- including resolving ``default``, which is what
    actually happened and which looks like extra strictness rather than a mistake.
    """
    view = next(v for v in _shipped_views() if str(v.declaration_id) == view_id)
    assert view.screening_profile is not None

    resolved = resolve_screening_profile(
        shipped, declared_profile_key=view.screening_profile, platform=platform
    )

    assert resolved.name == platform, (
        f"view {view_id!r} declares screening_profile={view.screening_profile!r}, which on a "
        f"{platform!r} host resolves to the {resolved.name!r} profile. A view must be screened "
        f"against the profile of the host that captured the frame; resolving {resolved.name!r} "
        f"means {platform!r} frames are tested for chrome that platform cannot produce (T292)."
    )


@pytest.mark.parametrize("view_id", _shipped_view_ids())
@pytest.mark.parametrize("platform", sorted(_PLATFORM_KEYS))
def test_the_resolved_profile_never_names_another_platforms_chrome(
    view_id: str, platform: str, shipped: ScreeningProfiles
) -> None:
    """The measured symptom, not just the mechanism: no foreign-platform category in the set.

    ``windows_capture_border`` in a reject set applied to an X11 frame is the whole finding in one
    string. Derived: a reject id is "another platform's" when its first token is a supported
    platform name that is not this host's -- the same token vocabulary
    ``techniques_for_category`` splits on, so a new ``macos_...`` id is covered without an edit.
    """
    view = next(v for v in _shipped_views() if str(v.declaration_id) == view_id)
    assert view.screening_profile is not None

    resolved = resolve_screening_profile(
        shipped, declared_profile_key=view.screening_profile, platform=platform
    )
    foreign = {
        category
        for category in resolved.reject
        if category.split("_")[0] in (_PLATFORM_KEYS - {platform})
    }

    assert not foreign, (
        f"on a {platform!r} host, view {view_id!r} resolves profile {resolved.name!r}, whose "
        f"reject set names another platform's chrome: {sorted(foreign)}. Those categories cannot "
        f"appear in a {platform!r} frame, so any withhold citing one is a test firing on "
        f"something else -- recorded as protection, and it was not."
    )


def test_on_this_very_host_the_resolved_profile_is_this_hosts_profile(
    shipped: ScreeningProfiles,
) -> None:
    """The same assertion against the host the suite is actually running on, not a parameter.

    The retro-audit's evidence is per-host: the runs recorded ``os=linux/x11`` and screened
    against the union profile anyway. This test would have been red on that box, on that day.
    """
    platform = detect_host_info().os.value

    for view in _shipped_views():
        assert view.screening_profile is not None
        resolved = resolve_screening_profile(
            shipped, declared_profile_key=view.screening_profile, platform=platform
        )
        assert resolved.name == platform, (
            f"this host reports platform {platform!r}, and view {view.declaration_id!r} resolves "
            f"the {resolved.name!r} profile"
        )


def test_the_historical_declaration_is_exactly_what_the_assertion_above_catches(
    shipped: ScreeningProfiles,
) -> None:
    """``default`` on a Linux host resolves the union profile -- reproduced, and named as the cause.

    This is the pre-98c71bb declaration, kept as an executable record of the mechanism rather than
    a sentence about it: the value is legal, the resolution is correct for what it says, and the
    result is that an X11 frame is tested for a Windows recording border. Nothing here is a bug in
    the resolver; the bug was declaring it.
    """
    resolved = resolve_screening_profile(
        shipped, declared_profile_key=ScreeningProfileDeclaration.DEFAULT, platform="linux"
    )

    assert resolved.name == "default"
    assert "windows_capture_border" in resolved.reject
    assert "windows_capture_border" not in shipped.profiles["linux"].reject


def test_an_undeclarable_screening_profile_value_fails_loudly_at_both_ends() -> None:
    """A value outside the vocabulary is an error at load *and* at resolution, not a third rule.

    Before T292 every unrecognised string fell through to "resolve by host platform", so
    ``platfrom``, ``Default`` and ``linux-x11`` all resolved to *something* and nothing said so.
    A silent fallback at the exact point where the gate decides what it is testing for is how a
    check comes to run against the wrong data for two whole runs.
    """
    with pytest.raises(ValidationError):
        ParityDeclaration(
            declaration_id="views.typo",  # type: ignore[arg-type]
            kind=DeclarationKind.VIEW,
            summary="A view with a mistyped screening profile.",
            parity_basis="Look at the map.",
            context="InGame",  # type: ignore[arg-type]
            capability_id="camera.control",  # type: ignore[arg-type]
            camera_requirements=CameraRequirements(
                mode=CameraMode.WORLD, zoom_range=(0.2, 1.0), target_must_be_revealed=True
            ),
            screening_profile="platfrom",
            output_schema={"type": "object"},
            introduced_in_version="2026.09.1",
        )

    profiles = load_screening_profiles()
    for undeclarable in ("platfrom", "linux", "Default", ""):
        with pytest.raises(CatalogError):
            resolve_screening_profile(profiles, declared_profile_key=undeclarable, platform="linux")


# --------------------------------------------------------------------------
# Negative control: the assertion above must be capable of failing
# --------------------------------------------------------------------------

#: A profile whose categories are built from words no *image* technique names, and which declare
#: no vocabulary. The pre-T299 rule called every one of these "addressed"; the rule now in force
#: calls them what they are.
_UNCOVERABLE_CATALOG = """
schema_version: 2
capture_scopes:
  unproven:
    description: nothing is excluded here
    excludes_origin: []
reject_definitions:
  firetuner_window:
    description: FireTuner.
    origin: separate_window
    text_keywords: []
    text_unmatchable: no observed window title anywhere
    text_settled_by: frame-scoped overlap evidence
  debug_overlay:
    description: A debug overlay.
    origin: in_frame
    text_keywords: []
    text_unmatchable: in-frame chrome has no window title
    text_settled_by: nothing about titles would help
  iridescent_shimmer:
    description: >
      A category deliberately named from words no technique in DefaultContentDetector looks for --
      not "border", not "overlay"/"debug" -- and with no declared vocabulary either.
    origin: separate_window
    text_keywords: []
    text_unmatchable: invented for a test
    text_settled_by: nothing; this category is not real
universal_reject: [firetuner_window]
profiles:
  linux:
    description: linux
    capture_scope: unproven
    reject: [firetuner_window, debug_overlay, iridescent_shimmer]
  default:
    description: default
    capture_scope: unproven
    reject: [firetuner_window, debug_overlay, iridescent_shimmer]
"""

#: T299's negative control, and the point of the whole task: **today's exact state**, written out.
#: Each category's keywords are its own name tokens -- which is what splitting the id on ``"_"``
#: produced -- and no witness string is declared, because none exists: no window title supplies
#: "window". This catalog must not load.
_TODAYS_STATE_CATALOG = """
schema_version: 2
capture_scopes:
  unproven:
    description: nothing is excluded here
    excludes_origin: []
reject_definitions:
  firetuner_window:
    description: FireTuner.
    origin: separate_window
    text_keywords:
      - [firetuner, window]
    text_basis: the category id, split on underscores -- which is what the old code did
  developer_console:
    description: A developer console.
    origin: in_frame
    text_keywords:
      - [developer, console]
    text_witnesses: ["Developer Console"]
    text_basis: an observed window title
universal_reject: [firetuner_window]
profiles:
  default:
    description: default
    capture_scope: unproven
    reject: [firetuner_window, developer_console]
"""


def test_todays_state_does_not_even_load(tmp_path: Path) -> None:
    """THE NEGATIVE CONTROL. Reproduce the shipped defect exactly and watch the new rule refuse it.

    ``firetuner_window`` declaring ``[firetuner, window]`` *is* the production state as of this
    morning -- the old code derived precisely that list by splitting the id, and the coverage
    guard passed on it. The only thing missing is a witness: a string the evidence source could
    produce that contains both words. None exists, because window titles do not contain the word
    "window", which is why the category never matched anything in 188 delivered frames.

    So the catalog is refused at load, naming the category and the group. ``developer_console`` in
    the same file is the control's control: its keywords are also its name tokens, it declares a
    witness that really produces them, and it loads -- proving the rule rejects unmatchability
    rather than rejecting name-shaped vocabularies.
    """
    catalog = tmp_path / "screening_profiles.yaml"
    catalog.write_text(_TODAYS_STATE_CATALOG, encoding="utf-8")

    with pytest.raises(CatalogError) as raised:
        load_screening_profiles(catalog)

    assert raised.value.detail["reject_id"] == "firetuner_window"
    assert "text_witnesses" in raised.value.message

    # Second arm, and the sharper one: a witness IS declared, and it is the most plausible string
    # anyone would reach for -- the application's own name. It still does not produce the group,
    # because the word "window" is not in it, and that is the whole defect in one assertion.
    with_useless_witness = catalog.read_text(encoding="utf-8").replace(
        "    text_basis: the category id, split on underscores -- which is what the old code did",
        '    text_witnesses: ["FireTuner"]\n    text_basis: the application name',
    )
    catalog.write_text(with_useless_witness, encoding="utf-8")
    with pytest.raises(CatalogError) as raised:
        load_screening_profiles(catalog)
    assert raised.value.detail["reject_id"] == "firetuner_window"
    assert raised.value.detail["keyword_group"] == ["firetuner", "window"]
    assert raised.value.detail["witness_tokens"] == [["firetuner"]]

    catalog.write_text(_TODAYS_STATE_CATALOG, encoding="utf-8")
    # The control's control: the same shape with a witness that really produces it loads fine.
    fixed = catalog.read_text(encoding="utf-8").replace(
        "    text_basis: the category id, split on underscores -- which is what the old code did",
        '    text_witnesses: ["FireTuner Window"]\n    text_basis: fabricated for this test',
    )
    catalog.write_text(fixed, encoding="utf-8")
    loaded = load_screening_profiles(catalog)
    assert loaded.reject_definitions["firetuner_window"].text_matchable


def test_a_definition_that_declares_no_vocabulary_and_no_reason_is_refused(tmp_path: Path) -> None:
    """Silence is the bug. A category may have no vocabulary; it may not fail to mention that."""
    catalog = tmp_path / "screening_profiles.yaml"
    catalog.write_text(
        _TODAYS_STATE_CATALOG.replace(
            "    text_keywords:\n      - [firetuner, window]\n"
            "    text_basis: the category id, split on underscores"
            " -- which is what the old code did\n",
            "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="must declare text_keywords"):
        load_screening_profiles(catalog)


def test_the_pre_t299_bare_description_shape_is_refused(tmp_path: Path) -> None:
    """The literal old file. A reject definition that is only prose carries no vocabulary at all,
    and loading it would silently reinstate "the id is its own keyword list"."""
    catalog = tmp_path / "screening_profiles.yaml"
    catalog.write_text(
        """
schema_version: 1
capture_scopes:
  unproven: {description: nothing, excludes_origin: []}
reject_definitions:
  firetuner_window: The FireTuner window or any of its child dialogs.
universal_reject: [firetuner_window]
profiles:
  default:
    description: default
    capture_scope: unproven
    reject: [firetuner_window]
""",
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="must be a mapping"):
        load_screening_profiles(catalog)


def test_a_keyword_the_tokeniser_cannot_produce_is_refused(tmp_path: Path) -> None:
    """A vocabulary is only real if the evidence source's own tokeniser can emit it.

    ``"firetuner-gui"`` and ``"FireTuner"`` both look like reasonable keywords and neither can
    ever appear in ``detected_text_tokens``: the production tokeniser lower-cases and splits on
    every non-alphanumeric character. Checked against
    :func:`~civsim_harness.host.port.window_text_tokens` itself rather than a copy of its rule.
    """
    catalog = tmp_path / "screening_profiles.yaml"
    for bad in ("firetuner-gui", "FireTuner"):
        catalog.write_text(
            _TODAYS_STATE_CATALOG.replace("[firetuner, window]", f'["{bad}"]'),
            encoding="utf-8",
        )
        with pytest.raises(CatalogError, match="tokeniser can"):
            load_screening_profiles(catalog)


def test_the_coverage_assertion_can_actually_fail(tmp_path: Path) -> None:
    """A positive control alone proves nothing; this is the negative one, at guard level.

    A profile carrying categories nothing addresses is exactly the shape the guard must report.
    Run against a synthetic catalog file so the repository's own data is never edited to prove a
    test works.

    What changed at T299: ``iridescent_shimmer`` used to be *addressable* whenever text evidence
    was available, because the declared-text rule applied to any id. Now it is addressed by
    nothing in either case, which is the honest answer for a category with no declared
    vocabulary -- and the same answer the real catalog gives for ``firetuner_window``.
    """
    catalog = tmp_path / "screening_profiles.yaml"
    catalog.write_text(_UNCOVERABLE_CATALOG, encoding="utf-8")
    loaded = load_screening_profiles(catalog)
    reject = loaded.profiles["linux"].reject

    uncovered = unaddressed_reject_categories(
        reject, available_techniques=ALL_TECHNIQUES, text_vocabulary=loaded.text_vocabulary
    )
    assert uncovered == {"firetuner_window", "iridescent_shimmer"}

    # Nothing is structurally excluded either, so the guard's own sum reports the same gap --
    # i.e. the assertion in test_every_reject_category_in_every_shipped_profile_is_matchable
    # would fail on this catalog.
    assert not loaded.structurally_excluded(loaded.profiles["linux"])

    # And the same gap, reported through the detector's own coverage declaration.
    detector = DefaultContentDetector()
    assert detector.addressable_categories(
        reject, text_evidence_available=True, text_vocabulary=loaded.text_vocabulary
    ) == {"debug_overlay"}


def test_a_category_with_no_tokens_at_all_is_addressed_by_nothing() -> None:
    """The degenerate id. It cannot be matched by any rule, so it must never read as covered."""
    assert techniques_for_category("") == frozenset()
    assert techniques_for_category("___") == frozenset()
    assert unaddressed_reject_categories(
        frozenset({"___"}), available_techniques=ALL_TECHNIQUES
    ) == {"___"}


def test_omitting_the_vocabulary_argument_addresses_nothing_rather_than_reverting(
    shipped: ScreeningProfiles,
) -> None:
    """The fail-closed default, which is what stops T299 being re-landed by a forgetful call site.

    ``techniques_for_category`` takes the vocabulary as a keyword argument with a default. The
    default had to be "no vocabulary at all", not "fall back to the id's own tokens": the second
    would restore the defect the first time someone wrote a call site without the argument, and
    it would restore it silently, which is how this got here.
    """
    assert techniques_for_category("developer_console") == frozenset()
    assert ScreeningTechnique.DECLARED_TEXT in techniques_for_category(
        "developer_console", text_vocabulary=shipped.text_vocabulary
    )
