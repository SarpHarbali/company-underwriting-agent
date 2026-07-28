"""Rerank a recall-oriented shortlist down to the companies a user likely meant.

Retrieval (`name_index.py`) is deliberately indiscriminate, so everything that
decides *which* company is shown first happens here, in Python, over a few
hundred rows. Doing it in Python rather than SQL is what makes several
similarity measures affordable at once: the shortlist is small, so the cost of
a measure is irrelevant and only its usefulness matters.

The target is recall@10 - the right company being *somewhere in the list the
user is shown* - not top-1 accuracy. The user picks; the ranking only has to
put the answer in front of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from rapidfuzz import fuzz
from rapidfuzz.distance import DamerauLevenshtein

from src.companies_house.name_index import NameMatch
from src.companies_house.names import normalised_forms

# Statuses that make a company a less likely search target. Bulk-data statuses
# are normalised to the API's hyphenated vocabulary at load time.
_INACTIVE_STATUSES = {
    "dissolved",
    "liquidation",
    "receivership",
    "converted-closed",
    "administration",
    "in-administration",
    "voluntary-arrangement",
}


@dataclass(frozen=True)
class Candidate:
    """One company the query might have meant, with the evidence for it."""

    company_number: str
    title: str
    status: str
    incorporation_date: date | None
    postcode: str | None
    score: float
    # The name row that actually matched. Equal to `title` unless the query
    # matched a former name, which the UI needs to say out loud.
    matched_name: str
    is_previous_name: bool
    # True when the query normalises to exactly this name - the only condition
    # under which resolution is allowed to skip disambiguation entirely.
    is_exact_match: bool


# How the name-similarity measures combine. Token-set carries the most weight
# because it is the one measure that handles the commonest real query shape -
# typing part of a longer registered name ("monzo" for MONZO BANK LIMITED) -
# without penalising the name for the words the user didn't type. The
# whole-string measures are folded in as a max rather than a sum: they are
# three views of the same question, so a candidate that any one of them rates
# highly deserves the credit, and averaging would just dilute it.
_W_TOKEN_SET = 0.45
_W_TOKEN_SORT = 0.15
_W_WHOLE_STRING = 0.40

# Bonuses and penalties, all small enough that they reorder near-ties without
# letting a weak name match overtake a strong one.
_EXACT_NAME_BONUS = 0.15
_EXACT_STEM_BONUS = 0.10
_LEADING_MATCH_BONUS = 0.06
_PREVIOUS_NAME_PENALTY = 0.10
_STATUS_BONUS = 0.08
_MAX_AGE_BONUS = 0.05
_MAX_AGE_YEARS = 100


def _ngrams(text: str, n: int) -> set[str]:
    padded = f" {text} "
    return {padded[i : i + n] for i in range(len(padded) - n + 1)}


def _ngram_overlap(a: str, b: str, n: int) -> float:
    """Dice coefficient over character n-grams - the same shape of measure
    `pg_trgm` uses for retrieval, recomputed here so ranking doesn't depend on
    which branch a candidate happened to arrive through.
    """
    grams_a, grams_b = _ngrams(a, n), _ngrams(b, n)
    if not grams_a or not grams_b:
        return 0.0
    return 2 * len(grams_a & grams_b) / (len(grams_a) + len(grams_b))


def _name_similarity(
    query_norm: str, query_stem: str, name_norm: str, name_stem: str
) -> float:
    """Blend of the similarity measures, on 0..1.

    Every measure is computed against both the full normalised names and their
    suffix-free forms, taking the better of the two. Legal suffixes are noise
    the user may or may not have typed, and a query of "acme" should not score
    worse against "ACME LIMITED" than against "ACME".
    """

    def best(measure) -> float:
        return max(measure(query_norm, name_norm), measure(query_stem, name_stem))

    token_set = best(lambda a, b: fuzz.token_set_ratio(a, b) / 100)
    token_sort = best(lambda a, b: fuzz.token_sort_ratio(a, b) / 100)
    damerau_levenshtein = best(DamerauLevenshtein.normalized_similarity)
    trigram = best(lambda a, b: _ngram_overlap(a, b, 3))
    bigram = best(lambda a, b: _ngram_overlap(a, b, 2))

    whole_string = max(damerau_levenshtein, trigram, bigram)
    return (
        _W_TOKEN_SET * token_set
        + _W_TOKEN_SORT * token_sort
        + _W_WHOLE_STRING * whole_string
    )


def _age_bonus(incorporation_date: date | None) -> float:
    """Small nudge toward older, more established companies.

    Scaled so it never outweighs name similarity or the active/inactive nudge -
    it only breaks ties between otherwise similar matches. Ramps linearly up to
    `_MAX_AGE_BONUS` at `_MAX_AGE_YEARS` old and is flat beyond that, so a
    200-year-old company isn't ranked above a 100-year-old one purely on age.
    """
    if incorporation_date is None:
        return 0.0
    age_years = date.today().year - incorporation_date.year
    return _MAX_AGE_BONUS * max(0.0, min(age_years, _MAX_AGE_YEARS)) / _MAX_AGE_YEARS


def score_match(query: str, match: NameMatch) -> Candidate:
    """Score one index row. Higher = more likely to be the company meant."""
    query_norm, query_stem = normalised_forms(query)
    name_norm, name_stem = normalised_forms(match.matched_name)

    score = _name_similarity(query_norm, query_stem, name_norm, name_stem)

    is_exact_match = bool(query_norm) and query_norm == name_norm
    if is_exact_match:
        score += _EXACT_NAME_BONUS
    elif query_stem and query_stem == name_stem:
        # Same name, different legal suffix ("acme ltd" vs "ACME LIMITED").
        # Worth nearly as much as an exact hit, but not quite: the difference
        # is occasionally real, as two distinct companies can differ only in
        # their suffix.
        score += _EXACT_STEM_BONUS
    elif name_norm.startswith(f"{query_norm} "):
        # A title that *starts* with the query is marginally likelier to be
        # the company meant than one that merely contains it somewhere.
        score += _LEADING_MATCH_BONUS

    if match.is_previous_name:
        # A former name is a real way to find a company - people search the
        # name they last dealt with - but when a query matches one company's
        # current name and another's old one, the current name wins.
        score -= _PREVIOUS_NAME_PENALTY

    status = (match.company_status or "unknown").lower()
    if status == "active":
        score += _STATUS_BONUS
    elif status in _INACTIVE_STATUSES:
        score -= _STATUS_BONUS

    score += _age_bonus(match.incorporation_date)

    return Candidate(
        company_number=match.company_number,
        title=match.current_name,
        status=status,
        incorporation_date=match.incorporation_date,
        postcode=match.postcode,
        score=round(max(0.0, score), 4),
        matched_name=match.matched_name,
        is_previous_name=match.is_previous_name,
        is_exact_match=is_exact_match,
    )


def rank_matches(query: str, matches: list[NameMatch]) -> list[Candidate]:
    """Score every row, keep the best row per company, and sort best first.

    Deduplication is by company number and happens *after* scoring rather than
    during retrieval, because a company can reach the shortlist through several
    of its names and only scoring reveals which one the user was typing.
    """
    best_by_number: dict[str, Candidate] = {}
    for match in matches:
        candidate = score_match(query, match)
        incumbent = best_by_number.get(candidate.company_number)
        if incumbent is None or candidate.score > incumbent.score:
            best_by_number[candidate.company_number] = candidate

    return sorted(best_by_number.values(), key=lambda c: c.score, reverse=True)
