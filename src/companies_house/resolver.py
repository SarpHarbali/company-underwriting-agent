from __future__ import annotations

import logging
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Protocol

from src.companies_house.client import CompaniesHouseClient, CompanyNotFoundError
from src.companies_house.name_index import NameRepository
from src.companies_house.names import normalised_forms, query_variants
from src.companies_house.ranking import Candidate, rank_matches

_log = logging.getLogger(__name__)


class QuerySuggester(Protocol):
    def __call__(self, text: str) -> str | None: ...


# UK company numbers are 8 digits or 2 letters followed by 6 digits.
_COMPANY_NUMBER_RE = re.compile(r"^(?:[A-Za-z]{2}\d{6}|\d{6,8})$")

_WEAK_MATCH_THRESHOLD = 0.6
_MAX_DISPLAYED_CANDIDATES = 10


@dataclass(frozen=True)
class SuggestedAlternative:
    query: str
    candidates: list[Candidate]


@dataclass(frozen=True)
class ResolutionResult:
    company_profile: dict[str, Any] | None = None
    candidates: list[Candidate] | None = None
    error: str | None = None
    corrected_query: str | None = None
    can_suggest: bool = False

    @property
    def is_resolved(self) -> bool:
        return self.company_profile is not None

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.candidates)


def _looks_like_company_number(text: str) -> bool:
    return bool(_COMPANY_NUMBER_RE.match(text.strip()))


def _ranked_matches(repository: NameRepository, query: str) -> list[Candidate]:
    query_norm, query_stem = normalised_forms(query)
    if not query_norm:
        return []
    matches = repository.find_candidates(
        query_norm, query_stem, query_variants(query_norm, query_stem)
    )
    return rank_matches(query, matches)


def _best_score(candidates: list[Candidate]) -> float:
    return candidates[0].score if candidates else 0.0


def _suggested_query(suggest_query: QuerySuggester, text: str) -> str | None:
    try:
        suggestion = suggest_query(text)
    except Exception:  # noqa: BLE001 - never let this break resolution
        _log.warning("Query suggester failed for %r", text, exc_info=True)
        return None

    if not suggestion:
        return None
    suggestion = suggestion.strip()
    if not suggestion or suggestion.lower() == text.strip().lower():
        return None
    return suggestion


def resolve_company(
    repository: NameRepository,
    client: CompaniesHouseClient,
    user_input: str,
    suggest_query: QuerySuggester | None = None,
) -> ResolutionResult:
    text = user_input.strip()
    if not text:
        return ResolutionResult(error="Please enter a company name or registration number.")

    if _looks_like_company_number(text):
        number = text.upper() if not text.isdigit() else text.zfill(8)
        try:
            profile = client.get_company_profile(number)
            return ResolutionResult(company_profile=profile)
        except CompanyNotFoundError:
            return ResolutionResult(
                error=f"No company found with registration number '{text}'."
            )

    try:
        candidates = _ranked_matches(repository, text)
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
        return ResolutionResult(error=f"Company name lookup failed: {exc}")

    best_score = _best_score(candidates)

    corrected_query: str | None = None
    if best_score < _WEAK_MATCH_THRESHOLD and suggest_query:
        corrected = _suggested_query(suggest_query, text)
        alternative_candidates = _ranked_matches(repository, corrected) if corrected else []
        alternative_score = _best_score(alternative_candidates)
        if (
            alternative_score > best_score
            and alternative_score >= _WEAK_MATCH_THRESHOLD
        ):
            corrected_query = corrected
            best_by_number = {
                candidate.company_number: candidate for candidate in candidates
            }
            for candidate in alternative_candidates:
                incumbent = best_by_number.get(candidate.company_number)
                if incumbent is None or candidate.score > incumbent.score:
                    best_by_number[candidate.company_number] = candidate
            candidates = sorted(
                best_by_number.values(),
                key=lambda candidate: candidate.score,
                reverse=True,
            )

    if not candidates:
        return ResolutionResult(error=f"No companies found matching '{text}'.")

    return ResolutionResult(
        candidates=candidates[:_MAX_DISPLAYED_CANDIDATES],
        corrected_query=corrected_query,
        can_suggest=suggest_query is not None and corrected_query is None,
    )


def suggest_alternative(
    repository: NameRepository,
    user_input: str,
    suggest_query: QuerySuggester,
    listed_numbers: Collection[str] = (),
) -> SuggestedAlternative | None:
    suggestion = _suggested_query(suggest_query, user_input.strip())
    if not suggestion:
        return None

    try:
        candidates = _ranked_matches(repository, suggestion)
    except Exception:  # noqa: BLE001 - the results already shown remain valid
        _log.warning("Lookup for suggested query %r failed", suggestion, exc_info=True)
        return None

    if _best_score(candidates) < _WEAK_MATCH_THRESHOLD:
        return None

    already_listed = set(listed_numbers)
    unseen = [
        candidate
        for candidate in candidates
        if candidate.company_number not in already_listed
    ]
    if not unseen:
        return None
    return SuggestedAlternative(
        query=suggestion,
        candidates=unseen[:_MAX_DISPLAYED_CANDIDATES],
    )
