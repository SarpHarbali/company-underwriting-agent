"""Resolve free-text user input (name or registration number) to a single company.

Identity resolution is kept deterministic and outside the LLM loop: getting the
*wrong* company silently is worse than any amount of extra friction, so every
name match is surfaced to the user rather than guessed at. Only a registration
number - which is an identity, not a guess at one - resolves on its own.

Name lookup runs against a local Postgres mirror of the Companies House bulk
data rather than the CH search endpoint. The endpoint is not fuzzy - one
mistyped letter can return nothing at all - and every retry costs a rate-limited
round trip, which put a hard ceiling on how much error tolerance was affordable.
Against a local index the whole approach inverts: retrieval casts a wide,
typo-tolerant net (`name_index.py`), and ranking sifts it in Python
(`ranking.py`). The API is still the source of truth for the company that gets
chosen - it is called for live details once, after selection.
"""

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
    """Suggests what a search query was meant to say, or None if unsure.

    Kept as a protocol so the resolver never imports an LLM client: the real
    implementation lives in `query_suggester.py`, and tests pass a lambda.
    """

    def __call__(self, text: str) -> str | None: ...


# UK company numbers: 8 digits, or 2 letters + 6 digits (e.g. SC123456, NI012345, OC123456).
_COMPANY_NUMBER_RE = re.compile(r"^(?:[A-Za-z]{2}\d{6}|\d{6,8})$")

# What a trustworthy match looks like on the ranking scale. Below this the
# query is weak enough to be worth second-guessing, and a corrected spelling is
# only adopted if it climbs back above it. Recall-oriented retrieval returns
# *something* for almost any string, so "beats the original" is far too low a
# bar on its own - the original may itself be junk.
_WEAK_MATCH_THRESHOLD = 0.6

# The brief asks for the best 5-10 candidates rather than a full shortlist.
# Retrieval deliberately fetches hundreds; showing hundreds would just move the
# disambiguation problem onto the user.
_MAX_DISPLAYED_CANDIDATES = 10


@dataclass(frozen=True)
class SuggestedAlternative:
    """A "did you mean...?" list, shown *alongside* the direct results.

    Deliberately not merged into `candidates`. These are scored against
    `query`, not against what the user typed, so the two sets are measured
    with different yardsticks and can't be meaningfully ranked against each
    other: searching "M&S" scores M&S ASSET MANAGEMENT at 0.8849, while
    MARKS AND SPENCER P.L.C. scores 1.2300 against "marks and spencer" but
    far lower against "M&S". Keeping them apart lets each list be ranked
    honestly and lets the UI say which query produced which.
    """

    query: str
    candidates: list[Candidate]


@dataclass(frozen=True)
class ResolutionResult:
    """Exactly one of `company_profile` or `candidates` is populated.

    A profile means the input was a registration number - the only input that
    identifies a company outright. Every name search ends in `candidates`, for
    the user to choose from.
    """

    company_profile: dict[str, Any] | None = None
    candidates: list[Candidate] | None = None
    error: str | None = None
    # Set when the original query matched nothing convincing and a corrected
    # spelling had to be used to find these candidates - the UI should say so.
    corrected_query: str | None = None
    # True when these candidates are worth second-guessing and nothing has
    # second-guessed them yet, so the UI can offer `suggest_alternative`.
    # False once a correction has already had its turn: the suggester has
    # spoken for this query, and asking it the same question twice would only
    # spend a second call to hear the same answer.
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
    """Retrieve a wide shortlist for `query` and rerank it, best first."""
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
    """What `text` was meant to say, or None if there's nothing useful to say.

    Swallows suggester failures rather than propagating them: this is an
    enhancement over a working deterministic path, so an outage or a bad key
    should cost the suggestion, not the search. Logged, not silent - degrading
    looks identical to the suggester simply declining, so a misconfigured
    client would otherwise vanish without trace.
    """
    try:
        suggestion = suggest_query(text)
    except Exception:  # noqa: BLE001 - never let this break resolution
        _log.warning("Query suggester failed for %r", text, exc_info=True)
        return None

    if not suggestion:
        return None
    suggestion = suggestion.strip()
    # A suggestion identical to what was typed tells us nothing.
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

    # A registration number is already an unambiguous identity, so it goes
    # straight to the API for live data - there is nothing for the index to
    # disambiguate and nothing to gain from a stale local copy.
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

    # Single-character slips are handled inside retrieval now, so a weak result
    # here means something edit distance can't reach: a mangled spelling, or a
    # colloquial name that simply isn't what the company is registered as
    # ("spoons" for Wetherspoon). That is exactly what the suggester is for.
    #
    # This is the one path that spends an LLM call unprompted, and it earns it:
    # without a correction there is no usable candidate list to show at all.
    # When the match is strong there *is* one, so the call waits to be asked
    # for - see `suggest_alternative`.
    corrected_query: str | None = None
    if best_score < _WEAK_MATCH_THRESHOLD and suggest_query:
        corrected = _suggested_query(suggest_query, text)
        alternative_candidates = _ranked_matches(repository, corrected) if corrected else []
        # Only trust a correction that both beats the original *and* stands up
        # on its own - otherwise a junk query whose best match scored 0.3 would
        # "correct" to different junk scoring 0.4 and be presented as a fix.
        if (
            _best_score(alternative_candidates) > best_score
            and _best_score(alternative_candidates) >= _WEAK_MATCH_THRESHOLD
        ):
            corrected_query = corrected
            by_number = {c.company_number: c for c in candidates}
            for candidate in alternative_candidates:
                incumbent = by_number.get(candidate.company_number)
                if incumbent is None or candidate.score > incumbent.score:
                    by_number[candidate.company_number] = candidate
            candidates = sorted(by_number.values(), key=lambda c: c.score, reverse=True)

    if not candidates:
        return ResolutionResult(error=f"No companies found matching '{text}'.")

    # A name search never resolves itself, however good the top match looks.
    # An exact, active, uniquely-matching name is strong evidence and it is
    # still ranked first - but evidence is not confirmation, and the cost of
    # the two outcomes is nowhere near symmetric: a wrong company silently
    # accepted produces a confident, fully-cited report about the wrong
    # business, which reads exactly like a right one. One click is a cheap
    # price for making the choice the user's.
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
    """What else the query might have meant, asked for after its results disappoint.

    The query matched well, but it may not have meant what it matched: "M&S"
    fits a dozen small M&S-prefixed firms perfectly while almost every user
    typing it means Marks and Spencer. Deliberately *not* computed during
    `resolve_company`. On the confident path the candidate list is usually the
    right one, so speculatively asking the LLM on every search buys an answer
    nobody reads. Waiting until the user says they can't find their company
    spends the call only where it might help - and makes the suggester's
    premise ("this search already failed") true rather than assumed.

    `listed_numbers` are the companies already on screen; a suggestion that
    only re-surfaces those is no suggestion at all.
    """
    suggestion = _suggested_query(suggest_query, user_input.strip())
    if not suggestion:
        return None

    try:
        candidates = _ranked_matches(repository, suggestion)
    except Exception:  # noqa: BLE001 - the results already shown remain valid
        _log.warning("Lookup for suggested query %r failed", suggestion, exc_info=True)
        return None

    # Held to the same bar as any other query: a suggestion whose own results
    # are weak is a worse answer than admitting there isn't one.
    if _best_score(candidates) < _WEAK_MATCH_THRESHOLD:
        return None

    already_listed = set(listed_numbers)
    unseen = [c for c in candidates if c.company_number not in already_listed]
    if not unseen:
        return None
    return SuggestedAlternative(query=suggestion, candidates=unseen[:_MAX_DISPLAYED_CANDIDATES])
