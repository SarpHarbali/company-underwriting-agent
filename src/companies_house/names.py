"""Name normalisation and query-variant generation for the local CH name index.

The same functions run at load time (building the index) and at query time, so
a stored name and a typed query that ought to match are guaranteed to have been
spelled the same way. That shared-implementation guarantee is the reason the
normalised forms live in an auxiliary table rather than in a Postgres
expression index: an expression index would need this logic restated as an
IMMUTABLE SQL function, and the two copies would drift the first time a suffix
was added to one of them.
"""

from __future__ import annotations

import re
import unicodedata

# Everything that isn't a letter or digit becomes a separator. Applied *after*
# the removals below, so the characters that should vanish already have.
_SEPARATORS = re.compile(r"[^a-z0-9]+")

# Dots and apostrophes disappear rather than becoming spaces: "P.L.C." should
# normalise to "plc" and "O'Neill" to "oneill", not "p l c" and "o neill".
_ELIDED = re.compile(r"[.'‘’`]")

# Legal-form suffixes, as token runs, longest first so "public limited company"
# is matched before the bare "company" inside it. Stripping these produces the
# "suffix-free" form the spec's trigram matching runs over as well as the full
# one: a query for "acme" and a registered "ACME LIMITED" share far more
# trigrams once the boilerplate both sides carry is gone.
_LEGAL_SUFFIX_RUNS: tuple[tuple[str, ...], ...] = (
    ("public", "limited", "company"),
    ("limited", "liability", "partnership"),
    ("community", "interest", "company"),
    ("limited", "partnership"),
    ("limited",),
    ("ltd",),
    ("plc",),
    ("llp",),
    ("llc",),
    ("lp",),
    ("cic",),
    ("cio",),
    ("unlimited",),
    ("incorporated",),
    ("inc",),
    ("company",),
    ("co",),
)

# One-edit variants are only generated for short queries. Trigram similarity
# degrades sharply as strings get shorter - a one-letter slip in "monzo" wrecks
# a larger share of its trigrams than the same slip in a four-word name - so
# short queries are exactly where n-gram recall needs the help, and they are
# also where enumerating edits stays cheap.
MAX_VARIANT_QUERY_LENGTH = 12
MAX_VARIANTS = 800

# Space is in the alphabet so a missing or spurious word break ("johnlewis")
# is reachable in one edit.
_VARIANT_ALPHABET = "abcdefghijklmnopqrstuvwxyz "


def normalise_name(text: str) -> str:
    """Casefold, strip accents and punctuation, and collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text)
    unaccented = "".join(c for c in decomposed if not unicodedata.combining(c))
    # "&" is spelled out rather than dropped so "Marks & Spencer" and "Marks
    # and Spencer" - which people type interchangeably - land on one string.
    expanded = unaccented.lower().replace("&", " and ")
    return _SEPARATORS.sub(" ", _ELIDED.sub("", expanded)).strip()


def strip_legal_suffix(normalised: str) -> str:
    """Drop trailing legal-form tokens: "acme trading co limited" -> "acme trading".

    Applied repeatedly, since names stack them. Never strips the last token: a
    company genuinely named "Limited" keeps a searchable form.
    """
    tokens = normalised.split()
    while len(tokens) > 1:
        for run in _LEGAL_SUFFIX_RUNS:
            if len(tokens) > len(run) and tuple(tokens[-len(run) :]) == run:
                tokens = tokens[: -len(run)]
                break
        else:
            break
    return " ".join(tokens)


def normalised_forms(text: str) -> tuple[str, str]:
    """The (full, suffix-free) normalised pair stored and queried for every name."""
    full = normalise_name(text)
    return full, strip_legal_suffix(full)


def query_variants(query_norm: str, query_stem: str) -> list[str]:
    """One-edit variants of both forms of the query.

    The suffix-free form is worth varying separately: "acme ldt" strips to
    nothing useful, but the same typo on a name whose suffix *did* parse leaves
    a stem that a single edit can still repair.
    """
    variants = one_edit_variants(query_norm)
    if query_stem and query_stem != query_norm:
        seen = set(variants)
        variants += [v for v in one_edit_variants(query_stem) if v not in seen]
    return variants[:MAX_VARIANTS]


def one_edit_variants(
    normalised: str,
    max_length: int = MAX_VARIANT_QUERY_LENGTH,
    max_variants: int = MAX_VARIANTS,
) -> list[str]:
    """Every string one edit away from `normalised`, for short inputs only.

    Covers the four single-character slips - adjacent transposition, deletion,
    substitution, insertion - which between them account for the large majority
    of real typos. Returned in rough likelihood order so that truncating at
    `max_variants` drops the least plausible corrections first.

    Returns nothing for longer inputs by design; the trigram branches already
    tolerate a single edit comfortably once a string is long enough, and the
    variant count grows linearly with length while the payoff falls.
    """
    if not normalised or len(normalised) > max_length:
        return []

    seen = {normalised}
    variants: list[str] = []

    def add(candidate: str) -> None:
        # A variant that isn't already in normal form can't match anything in
        # the index, which only ever stores normalised names.
        if candidate != normalise_name(candidate) or candidate in seen:
            return
        seen.add(candidate)
        variants.append(candidate)

    for i in range(len(normalised) - 1):
        add(normalised[:i] + normalised[i + 1] + normalised[i] + normalised[i + 2 :])

    for i in range(len(normalised)):
        add(normalised[:i] + normalised[i + 1 :])

    for i in range(len(normalised)):
        for letter in _VARIANT_ALPHABET:
            add(normalised[:i] + letter + normalised[i + 1 :])

    for i in range(len(normalised) + 1):
        for letter in _VARIANT_ALPHABET:
            add(normalised[:i] + letter + normalised[i:])

    return variants[:max_variants]
