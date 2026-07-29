from __future__ import annotations

import re
import unicodedata

_SEPARATORS = re.compile(r"[^a-z0-9]+")

_ELIDED = re.compile(r"[.'‘’`]")

# Longest first so multi-token legal forms match before "company".
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

MAX_VARIANT_QUERY_LENGTH = 12
MAX_VARIANTS = 800

_VARIANT_ALPHABET = "abcdefghijklmnopqrstuvwxyz "


def normalise_name(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    unaccented = "".join(c for c in decomposed if not unicodedata.combining(c))
    expanded = unaccented.lower().replace("&", " and ")
    return _SEPARATORS.sub(" ", _ELIDED.sub("", expanded)).strip()


def strip_legal_suffix(normalised: str) -> str:
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
    full = normalise_name(text)
    return full, strip_legal_suffix(full)


def query_variants(query_norm: str, query_stem: str) -> list[str]:
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
    if not normalised or len(normalised) > max_length:
        return []

    seen = {normalised}
    variants: list[str] = []

    def add(candidate: str) -> None:
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
