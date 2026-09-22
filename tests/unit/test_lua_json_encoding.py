"""The hand-rolled Lua JSON encoders must never sever a UTF-8 sequence.

The defect this closes (2026-09-22, three consecutive dead live runs). Every live run died
~1.4 s in, on the *first* observation read, before any decision existed::

    UnicodeDecodeError: 'utf-8' codec can't decode byte 0xc4 in position 1663
      at src/civsim_harness/nexus/codec.py:137

The failing frame was ``great_people.state``. The recruitable Great Artist is
``Kamāl ud-Dīn Behzād``, and the emitted JSON contained ``"name":"Kam\\xc4\\u0081l ud-D\\xc4\\xabn
Behz\\xc4\\u0081d"`` -- a raw lead byte ``C4`` followed by the *literal six-character text*
``\\u0081``.

**Lua patterns match BYTES, not characters.** Every ``lua/**/*.lua`` file's hand-rolled
``CivSim_JsonEncode`` (and ``nexus.sentinels.LUA_JSON_PRELUDE``, the one copy carried by the Lua
bodies embedded in Python) escaped via ``value:gsub('[%c"\\\\]', ...)``. ``%c`` is ``iscntrl()``
under the *client's* locale, which includes the C1 range ``0x80-0x9F``. UTF-8 continuation bytes
live in ``0x80-0xBF``, so the two ranges overlap:

- ``ā`` = ``C4 81`` -- the trailing ``0x81`` **is** C1, is escaped to the text ``\\u0081``, and the
  lead byte ``C4`` is left raw. The sequence is severed and the frame is undecodable.
- ``ī`` = ``C4 AB`` -- the trailing ``0xAB`` is **not** C1, passes through untouched, and survives.

Both occur in the same name, in the same frame, which is why that one string is the fixture here:
it carries its own positive control. Any game string with a byte in ``0x80-0x9F`` killed the tuner
connection -- most of Latin Extended-A, so leader, city, city-state and great-person names were all
live hazards.

**Why this test simulates the locale, and why that is not a cheat.** ``%c``'s meaning is decided by
``iscntrl()`` in the running process's locale. Measured on this host: *no installed locale makes
``%c`` match ``0x81``* -- ``C``, ``C.utf8``, ``POSIX`` and every installed ``*.utf8`` locale all
report ``0x81`` unmatched, because glibc's UTF-8 ctype tables classify no byte above ``0x7F``.
(glibc's 8-bit ``i18n`` definitions *do* put ``U+0080..U+009F`` in ``cntrl``; no such locale is
installed here.) So a test that merely encoded the name under the ambient locale would **pass
against the broken encoder** -- a check that cannot fail, which is the one thing this project
refuses to ship. :data:`_SIMULATE_CLIENT_LOCALE` therefore reproduces the *documented production
condition* directly, by widening the encoder's own pattern to include C1 exactly as the client's
``iscntrl()`` does, and :func:`_encoder` asserts the simulation actually fired -- so if the
pattern text ever drifts out from under the wrapper, this test fails loudly instead of quietly
going vacuous.

**Why the fix guards in the callback rather than rewriting the character class.** Restricting the
class to ASCII controls needs ``0x00`` in the pattern, and that byte is spelled ``%z`` in Lua 5.1
and ``\\0`` in 5.2+ -- with no single spelling valid in both. The client's embedded Lua version is
**not** what ``lupa`` provides here (measured: ``lupa`` 2.8 embeds ``Lua 5.5``), so this suite
could not discriminate a wrong choice: a 5.5-only class would pass every test here and break every
observation on the client, which is strictly worse than one broken frame. Guarding inside the
replacement function instead -- ``if string.byte(c) >= 0x80 then return c end`` -- is identical in
every Lua from 5.0 to 5.5 and in LuaJIT, touches no pattern syntax, and makes the behaviour
*locale-independent*: whatever ``%c`` matched, no byte above ``0x7F`` is ever escaped. Bytes below
``0x80`` are unaffected, since ASCII controls are ``iscntrl()`` in every locale and printable ASCII
is ``iscntrl()`` in none.

**Why "all 27 copies are identical" is a test and not a convention.** The sandbox exposes no
``require``, ``io`` or ``debug`` and no JSON library, so no shared Lua module can ever be factored
out and required (``lua/ingame/great_people.lua``'s own header; ``capability/executor.py``'s "No
shared Lua helper is ever injected"). One definition with 27 *verified-identical* instances is the
closest structural equivalent available: :func:`test_every_copy_of_the_encoder_is_byte_identical`
makes a divergent edit, or a 28th file carrying the old text, fail the suite. That is the control;
editing 27 files by hand and hoping is the rule it replaces.

Runs in both modes on purpose. The structural tests read text and need no Lua runtime, so they run
under the plain suite. The behavioural tests need ``uv run --with lupa``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE

try:  # pragma: no cover - exercised by which suite mode is running
    import lupa
except ImportError:  # pragma: no cover
    lupa = None  # type: ignore[assignment]

requires_lupa = pytest.mark.skipif(
    lupa is None, reason="needs an embedded Lua: uv run --with lupa pytest <this file>"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
LUA_ROOT = REPO_ROOT / "lua"

#: The recruitable Great Artist whose name killed three consecutive live runs. Carries **both**
#: halves of the discriminating pair: ``ā`` (``C4 81``, trailing byte in C1 -> severed by the bug)
#: and ``ī`` (``C4 AB``, trailing byte outside C1 -> survived the bug).
GREAT_ARTIST = "Kamāl ud-Dīn Behzād"

#: The whole file-level ``CivSim_JsonEncode`` definition, from its ``local function`` line to the
#: first column-0 ``end``. Every ``lua/**/*.lua`` file carries exactly one.
_ENCODER_RE = re.compile(
    r"^local function CivSim_JsonEncode\(value\)\n.*?^end\n", re.MULTILINE | re.DOTALL
)

#: Reproduces the client's locale, in which ``iscntrl()`` is true across the C1 range, by widening
#: the encoder's own ``[%c"\]`` class to include ``0x80-0x9F`` explicitly. No locale installed on
#: this host does that (see the module docstring), so without this the negative case cannot fail.
#: ``CIVSIM_LOCALE_SIMULATED`` is the simulation's own positive control: :func:`_encoder` requires
#: it to have fired, so a pattern the wrapper no longer recognises is a loud failure rather than a
#: silently vacuous pass.
_SIMULATE_CLIENT_LOCALE = rb"""
CIVSIM_LOCALE_SIMULATED = 0
local real_gsub = string.gsub
string.gsub = function(s, pat, repl, n)
    if pat == '[%c"\\]' then
        CIVSIM_LOCALE_SIMULATED = CIVSIM_LOCALE_SIMULATED + 1
        pat = '[%c\128-\159"\\]'
    end
    return real_gsub(s, pat, repl, n)
end
"""


def _lua_sources() -> list[tuple[str, bytes]]:
    """Every encoder in the tree, discovered rather than listed.

    The 27 ``lua/**/*.lua`` files plus ``nexus.sentinels.LUA_JSON_PRELUDE``'s
    ``civsim_json_value`` -- the copy carried by the Lua bodies embedded directly in Python
    modules (the save call, the leader-selection write and read-back, the version probe), which
    had the identical defect and is reached by a different code path entirely.

    Enumerating the tree is the point: a file added tomorrow is covered without anyone
    remembering to add it here.
    """
    sources = [
        (
            str(path.relative_to(REPO_ROOT)),
            path.read_bytes() + b"\nCIVSIM_ENCODE = CivSim_JsonEncode\n",
        )
        for path in sorted(LUA_ROOT.rglob("*.lua"))
    ]
    sources.append(
        (
            "src/civsim_harness/nexus/sentinels.py::LUA_JSON_PRELUDE",
            LUA_JSON_PRELUDE.encode("utf-8") + b"\nCIVSIM_ENCODE = civsim_json_value\n",
        )
    )
    return sources


ENCODER_SOURCES = _lua_sources()
ENCODER_IDS = [name for name, _ in ENCODER_SOURCES]


def _encoder(source: bytes) -> Any:
    """One encoder, loaded into a fresh runtime that behaves like the client's locale."""
    runtime = lupa.LuaRuntime(encoding=None)
    runtime.execute(_SIMULATE_CLIENT_LOCALE)
    runtime.execute(source)
    encode = runtime.globals()[b"CIVSIM_ENCODE"]
    assert encode is not None, "the encoder did not load"

    def call(value: str) -> bytes:
        encoded = encode(value.encode("utf-8"))
        # The simulation's positive control: if the wrapper never recognised the encoder's
        # pattern, this test proves nothing and must say so rather than pass.
        assert runtime.globals()[b"CIVSIM_LOCALE_SIMULATED"] > 0, (
            "the client-locale simulation never fired -- the encoder's character class is no "
            "longer the text _SIMULATE_CLIENT_LOCALE rewrites, so this assertion is vacuous"
        )
        return bytes(encoded)

    return call


def _round_trip(encode: Any, value: str) -> str:
    """Encode *value*, then take it back through exactly what production does.

    ``nexus/codec.py:137`` decodes the frame payload as strict UTF-8; that decode is where all
    three runs died, so it is reproduced here rather than approximated.
    """
    payload = encode(value)
    text = payload.decode("utf-8")  # the production failure point
    return json.loads(text)


# ---------------------------------------------------------------------------
# The defect, and its own positive control -- both from one production string
# ---------------------------------------------------------------------------


@requires_lupa
@pytest.mark.parametrize("source", [s for _, s in ENCODER_SOURCES], ids=ENCODER_IDS)
def test_the_great_artists_name_survives_the_encoder(source: bytes) -> None:
    """The exact string that killed three live runs must round-trip intact."""
    assert _round_trip(_encoder(source), GREAT_ARTIST) == GREAT_ARTIST


@requires_lupa
@pytest.mark.parametrize("source", [s for _, s in ENCODER_SOURCES], ids=ENCODER_IDS)
def test_a_continuation_byte_inside_the_c1_range_is_not_escaped(source: bytes) -> None:
    """The bug case, isolated: ``ā`` is ``C4 81`` and ``0x81`` is a C1 control byte.

    Before the fix this produced a raw ``C4`` followed by the literal text ``\\u0081``.
    """
    encode = _encoder(source)
    assert _round_trip(encode, "ā") == "ā"
    payload = encode("ā")
    assert b"\xc4\x81" in payload, "the UTF-8 sequence was not emitted intact"
    assert rb"\u0081" not in payload, "the continuation byte was escaped and the sequence severed"


@requires_lupa
@pytest.mark.parametrize("source", [s for _, s in ENCODER_SOURCES], ids=ENCODER_IDS)
def test_a_continuation_byte_outside_the_c1_range_still_survives(source: bytes) -> None:
    """The positive control, isolated: ``ī`` is ``C4 AB`` and ``0xAB`` is *not* C1.

    This already held before the fix. It is here so that a fix which broke it -- by touching the
    wrong range, or by mangling high bytes some other way -- is visible rather than silent.
    """
    encode = _encoder(source)
    assert _round_trip(encode, "ī") == "ī"
    assert b"\xc4\xab" in encode("ī")


@requires_lupa
@pytest.mark.parametrize("source", [s for _, s in ENCODER_SOURCES], ids=ENCODER_IDS)
def test_no_byte_above_ascii_is_ever_escaped(source: bytes) -> None:
    """Sweep the whole hazard range rather than the two bytes that happened to be in the name.

    ``0x80-0x9F`` is what the client's ``%c`` over-matched; the sweep runs to ``0xBF`` (every
    legal UTF-8 continuation byte) and includes lead bytes, so the property under test is "no byte
    above ASCII is escaped", not "these two names work".
    """
    encode = _encoder(source)
    for codepoint in (0x80, 0x81, 0x8F, 0x9F, 0xA0, 0xAB, 0xBF, 0xC4, 0x100, 0x101, 0x12B, 0x20AC):
        value = f"x{chr(codepoint)}y"
        assert _round_trip(encode, value) == value, f"U+{codepoint:04X} did not survive"


# ---------------------------------------------------------------------------
# The direction that would be catastrophic to "fix" -- pinned explicitly
# ---------------------------------------------------------------------------


@requires_lupa
@pytest.mark.parametrize("source", [s for _, s in ENCODER_SOURCES], ids=ENCODER_IDS)
def test_ascii_controls_quotes_and_backslashes_are_still_escaped(source: bytes) -> None:
    """An encoder that simply stopped escaping would pass every test above.

    So the required escapes are pinned here, as raw output text rather than only as a round-trip:
    a round-trip alone is satisfied by anything ``json.loads`` accepts, and the point is *which*
    bytes must never reach the wire unescaped.
    """
    encode = _encoder(source)
    #: Control bytes must not appear raw *anywhere* in the output -- an unescaped one is invalid
    #: JSON and, for ``\x00``, would also break ``nexus/codec.py``'s NUL framing.
    controls = {
        "\n": rb"\n",
        "\r": rb"\r",
        "\t": rb"\t",
        "\x00": rb"\u0000",
        "\x01": rb"\u0001",
        "\x07": rb"\u0007",
        "\x1f": rb"\u001f",
        "\x7f": rb"\u007f",
    }
    for raw, escape in controls.items():
        payload = encode(f"a{raw}b")
        assert escape in payload, f"{raw!r} was not escaped as {escape!r} -- got {payload!r}"
        assert raw.encode("utf-8") not in payload, f"{raw!r} reached the output unescaped"
        assert _round_trip(encode, f"a{raw}b") == f"a{raw}b"

    #: ``"`` and ``\`` cannot be checked by absence -- the output is quote-delimited and the
    #: escapes themselves are backslashes -- so they are pinned on the exact body text instead.
    assert encode('a"b') == rb'"a\"b"'
    assert encode("a\\b") == rb'"a\\b"'
    assert _round_trip(encode, 'a"b') == 'a"b'
    assert _round_trip(encode, "a\\b") == "a\\b"


@requires_lupa
@pytest.mark.parametrize("source", [s for _, s in ENCODER_SOURCES], ids=ENCODER_IDS)
def test_a_name_mixing_every_class_at_once_round_trips(source: bytes) -> None:
    """Controls, quotes, backslashes and both halves of the pair in one string."""
    value = f'{GREAT_ARTIST}\t"quoted" C:\\path\nātail'
    assert _round_trip(_encoder(source), value) == value


# ---------------------------------------------------------------------------
# One definition, 27 verified-identical instances (the structural control)
# ---------------------------------------------------------------------------


def test_every_lua_file_defines_the_shared_encoder() -> None:
    """No file may dodge the identity check below by naming its encoder something else."""
    missing = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted(LUA_ROOT.rglob("*.lua"))
        if not _ENCODER_RE.search(path.read_text(encoding="utf-8"))
    ]
    assert missing == [], f"no CivSim_JsonEncode found in: {missing}"


def test_every_copy_of_the_encoder_is_byte_identical() -> None:
    """The sandbox forbids a shared Lua module, so this assertion is the shared helper.

    27 hand-maintained copies are 27 chances to fix 26 of them. Byte-identity makes the copies one
    definition: a divergent edit, or a new file carrying the pre-fix text, fails here.
    """
    bodies: dict[str, list[str]] = {}
    for path in sorted(LUA_ROOT.rglob("*.lua")):
        match = _ENCODER_RE.search(path.read_text(encoding="utf-8"))
        assert match is not None, path
        digest = hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()
        bodies.setdefault(digest, []).append(str(path.relative_to(REPO_ROOT)))
    assert len(bodies) == 1, f"CivSim_JsonEncode has diverged across files: {bodies}"


def test_the_high_byte_guard_is_present_in_every_encoder() -> None:
    """The one line the fix turns on, asserted as text in all 28 copies.

    The behavioural tests above are the real proof, but they need ``lupa``. This runs under the
    plain suite, so the guard cannot be removed on a machine that never executes Lua.
    """
    guard = "if string.byte(c) >= 0x80 then return c end"
    for path in sorted(LUA_ROOT.rglob("*.lua")):
        assert guard in path.read_text(encoding="utf-8"), f"{path} lost the high-byte guard"
    assert guard in LUA_JSON_PRELUDE, "nexus.sentinels.LUA_JSON_PRELUDE lost the high-byte guard"
