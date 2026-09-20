"""The fixed predicate symbol table (T035 load-time gate; T106 evaluator binds here).

Predicates (``availability_predicate``, ``verification_predicate`` on an action
declaration) are restricted boolean expressions, not arbitrary code
(contracts/capability-catalog.md validation 6: "Predicates reference only
symbols the evaluator exposes -- no arbitrary evaluation."). This module
defines the root namespaces a predicate may reach into, matching
``catalogs/README.md`` SS4 ("The predicate language (T106's fixed symbol
table)") exactly at the time this loader was written, so the catalog author,
this load-time gate, and T106's restricted evaluator all bind to one table
instead of three that can drift apart.

This module performs a *syntactic* check only: it rejects a predicate that
reaches for a root namespace nobody declared. It does not parse the full
predicate grammar (operators, literals, membership) or validate the deeper
per-field vocabulary within a namespace (e.g. that ``unit.frobnicate`` is not
a real field) -- that is T106's job at evaluation time, once a concrete
declaration's subject is bound. Catching the wrong-namespace mistake at catalog
load time is still worth doing on its own: it is the exact failure mode that
turns into "the game itself would have refused this" turning into "the harness
silently evaluated a predicate against nothing" if left to run time.
"""

from __future__ import annotations

import re

# catalogs/README.md SS4 "Namespaces exposed to every predicate".
EXPOSED_PREDICATE_NAMESPACES: frozenset[str] = frozenset(
    {
        "game",
        "player",
        "unit",
        "city",
        "target",
        "other_player",
        "congress",
        "great_person",
        "spy",
        "prompt",
        "camera",
    }
)

# catalogs/README.md SS4 "observed_*": a dynamic namespace, one flat symbol per
# harness-bound pre-execution snapshot a verification predicate actually
# needs (e.g. ``observed_turn_number``), rather than an enumerable fixed set.
OBSERVED_PREFIX = "observed_"

# catalogs/README.md SS4 "Operators" and "Literals": these are grammar, not
# symbols, and must never be flagged as an unexposed namespace.
_PREDICATE_KEYWORDS: frozenset[str] = frozenset({"and", "or", "not", "in", "true", "false", "null"})

# String literals are stripped before symbol extraction so that quoted text
# (e.g. `player.current_research == "Bronze Working"`) is never mistaken for
# identifier chains.
_STRING_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# An identifier chain: `unit`, `target`, `unit.reachable_plots`,
# `observed_turn_number`. Only the root (the segment before the first `.`,
# or the whole token when there is no `.`) is checked against the exposed
# table -- the deeper field vocabulary is T106's concern, not this gate's.
_IDENTIFIER_CHAIN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def unknown_predicate_symbols(predicate: str) -> frozenset[str]:
    """Return the root symbols *predicate* uses that are not exposed.

    An empty result means every root namespace the predicate touches is one
    of :data:`EXPOSED_PREDICATE_NAMESPACES` or an ``observed_*`` snapshot.
    """
    stripped = _STRING_LITERAL_RE.sub(" ", predicate)
    unknown: set[str] = set()
    for match in _IDENTIFIER_CHAIN_RE.finditer(stripped):
        root = match.group(0).split(".", 1)[0]
        if root.lower() in _PREDICATE_KEYWORDS:
            continue
        if root in EXPOSED_PREDICATE_NAMESPACES:
            continue
        if root.startswith(OBSERVED_PREFIX):
            continue
        unknown.add(root)
    return frozenset(unknown)
