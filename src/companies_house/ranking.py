from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from rapidfuzz import fuzz
from rapidfuzz.distance import DamerauLevenshtein

from src.companies_house.name_index import NameMatch
from src.companies_house.names import normalised_forms

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
    company_number: str
    title: str
    status: str
    incorporation_date: date | None
    postcode: str | None
    score: float
    matched_name: str
    is_previous_name: bool


# Token-set similarity gives partial registered names the most influence.
_W_TOKEN_SET = 0.45
_W_TOKEN_SORT = 0.15
_W_WHOLE_STRING = 0.40

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
    grams_a, grams_b = _ngrams(a, n), _ngrams(b, n)
    if not grams_a or not grams_b:
        return 0.0
    return 2 * len(grams_a & grams_b) / (len(grams_a) + len(grams_b))


def _name_similarity(
    query_norm: str, query_stem: str, name_norm: str, name_stem: str
) -> float:
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
    if incorporation_date is None:
        return 0.0
    age_years = date.today().year - incorporation_date.year
    return _MAX_AGE_BONUS * max(0.0, min(age_years, _MAX_AGE_YEARS)) / _MAX_AGE_YEARS


def score_match(query: str, match: NameMatch) -> Candidate:
    query_norm, query_stem = normalised_forms(query)
    name_norm, name_stem = normalised_forms(match.matched_name)

    score = _name_similarity(query_norm, query_stem, name_norm, name_stem)

    is_exact_match = bool(query_norm) and query_norm == name_norm
    if is_exact_match:
        score += _EXACT_NAME_BONUS
    elif query_stem and query_stem == name_stem:
        score += _EXACT_STEM_BONUS
    elif name_norm.startswith(f"{query_norm} "):
        score += _LEADING_MATCH_BONUS

    if match.is_previous_name:
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
    )


def rank_matches(query: str, matches: list[NameMatch]) -> list[Candidate]:
    best_by_number: dict[str, Candidate] = {}
    for match in matches:
        candidate = score_match(query, match)
        incumbent = best_by_number.get(candidate.company_number)
        if incumbent is None or candidate.score > incumbent.score:
            best_by_number[candidate.company_number] = candidate

    return sorted(best_by_number.values(), key=lambda c: c.score, reverse=True)
