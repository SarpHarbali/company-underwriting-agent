"""Resolve free-text user input (name or registration number) to a single company.

Identity resolution is kept deterministic and outside the LLM loop: getting the
*wrong* company silently is worse than any amount of extra friction, so ambiguous
matches are always surfaced to the user rather than guessed at.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import Any, Protocol

from src.companies_house.client import CompaniesHouseClient, CompanyNotFoundError

_log = logging.getLogger(__name__)


class QuerySuggester(Protocol):
    """Suggests what a search query was meant to say, or None if unsure.

    Kept as a protocol so the resolver never imports an LLM client: the real
    implementation lives in `query_suggester.py`, and tests pass a lambda.
    """

    def __call__(self, text: str) -> str | None: ...

# UK company numbers: 8 digits, or 2 letters + 6 digits (e.g. SC123456, NI012345, OC123456).
_COMPANY_NUMBER_RE = re.compile(r"^(?:[A-Za-z]{2}\d{6}|\d{6,8})$")

_INACTIVE_STATUSES = {"dissolved", "liquidation", "receivership", "converted-closed", "administration"}

# What a trustworthy match looks like. Used for both halves of the correction
# decision: below this the original query is weak enough to be worth second-
# guessing, and a corrected spelling is only adopted if it climbs back above
# it. Companies House's search isn't fuzzy and returns real-but-irrelevant
# hits for almost any garbled string, so "beat the original" is far too low a
# bar on its own - the original may itself be junk.
_WEAK_MATCH_THRESHOLD = 0.6

# Every variant tried is a live API call. Transposition count grows with query
# length, but a transposition is *least* likely to be the whole problem on a
# long multi-word query - so the cap keeps short-query typos fully covered
# (they generate fewer variants than this anyway) and hands the long tail to
# the query suggester, which is one call instead of dozens.
_MAX_TYPO_LOOKUPS = 12


@dataclass(frozen=True)
class Candidate:
    company_number: str
    title: str
    status: str
    company_type: str
    date_of_creation: str | None
    address_snippet: str | None
    score: float


@dataclass(frozen=True)
class SuggestedAlternative:
    """A "did you mean...?" list, shown *alongside* the direct results.

    Deliberately not merged into `candidates`. These are scored against
    `query`, not against what the user typed, so the two sets are measured
    with different yardsticks and can't be meaningfully ranked against each
    other: searching "M&S" scores M&S ASSET MANAGEMENT at 0.7865, while
    MARKS AND SPENCER P.L.C. scores 0.8300 against "marks and spencer" but
    0.2337 against "M&S". Keeping them apart lets each list be ranked
    honestly and lets the UI say which query produced which.
    """

    query: str
    candidates: list[Candidate]


@dataclass(frozen=True)
class ResolutionResult:
    """Exactly one of `company_profile` or `candidates` is populated."""

    company_profile: dict[str, Any] | None = None
    candidates: list[Candidate] | None = None
    error: str | None = None
    # Set when the original query returned nothing and a corrected spelling
    # had to be used to find these candidates - the UI should say so.
    corrected_query: str | None = None
    # Set when the query found good matches but may not have meant them -
    # "M&S" finding M&S Asset Management when Marks and Spencer was meant.
    suggestion: SuggestedAlternative | None = None

    @property
    def is_resolved(self) -> bool:
        return self.company_profile is not None

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.candidates)


def _looks_like_company_number(text: str) -> bool:
    return bool(_COMPANY_NUMBER_RE.match(text.strip()))


_MAX_NAME_SCORE = 0.70
# Small discount for a token match that isn't at the start of the title, so
# "Wetherspoon Ltd" still edges out "J D Wetherspoon plc" on name alone -
# but not so large that status and age can't overturn it.
_NON_LEADING_TOKEN_PENALTY = 0.06
_MAX_AGE_BONUS = 0.05
_MAX_AGE_YEARS = 100


def _age_bonus(date_of_creation: str | None) -> float:
    """Small nudge toward older, more established companies.

    Scaled so it never outweighs name similarity or the active/inactive
    nudge - it only breaks ties between otherwise similar matches. Ramps
    linearly up to `_MAX_AGE_BONUS` at `_MAX_AGE_YEARS` old and flat beyond
    that, so a 200-year-old company isn't ranked above a 100-year-old one
    purely on age.
    """
    if not date_of_creation:
        return 0.0
    try:
        incorporation_year = int(date_of_creation[:4])
    except ValueError:
        return 0.0

    age_years = date.today().year - incorporation_year
    return _MAX_AGE_BONUS * max(0.0, min(age_years, _MAX_AGE_YEARS)) / _MAX_AGE_YEARS


def _token_run_start(query_tokens: list[str], title_tokens: list[str]) -> int | None:
    """Index at which `query_tokens` appear as a contiguous run inside
    `title_tokens`, or None if they don't appear at all.
    """
    if not query_tokens:
        return None
    span = len(query_tokens)
    for start in range(len(title_tokens) - span + 1):
        if title_tokens[start : start + span] == query_tokens:
            return start
    return None


def _name_similarity(q: str, t: str) -> float:
    """How well a (lowercased, stripped) query matches a company title.

    A query matching a run of the title's *tokens* is a full name match no
    matter what surrounds it: searching "monzo" should rate "Monzo Bank
    Limited" and "Monzo Tyres Ltd" equally on name alone, leaving status and
    age to break the tie. Scoring the whole strings against each other
    instead - as a bare SequenceMatcher ratio does - quietly penalises the
    longer title (0.4348 vs 0.5000 for those two), so results end up ordered
    by name length rather than relevance.

    The run needn't be at the front. People search the name they know, not
    the registered one: "wetherspoon" should find "J D WETHERSPOON PLC", and
    leading-token-only matching buried it below two dissolved "Wetherspoon &
    Partner" companies. A non-leading match still scores slightly lower,
    since a title that *starts* with the query is marginally likelier to be
    the company meant. Anything with no token run falls back to fuzzy
    whole-string similarity, which still catches partial words and typos.
    """
    query_tokens = q.split()
    title_tokens = t.split()
    run_start = _token_run_start(query_tokens, title_tokens)

    if run_start == 0:
        score = _MAX_NAME_SCORE
    elif run_start is not None:
        score = _MAX_NAME_SCORE - _NON_LEADING_TOKEN_PENALTY
    else:
        score = _MAX_NAME_SCORE * SequenceMatcher(None, q, t).ratio()
        if t.startswith(q) or q.startswith(t):
            score += 0.07

    if t == q:
        score += 0.15

    return score


def _score_candidate(query: str, title: str, status: str, date_of_creation: str | None = None) -> float:
    """Higher score = more likely to be the company the user meant.

    Name similarity dominates; status and incorporation age are small nudges
    (not filters) so a strong exact-name match on a dissolved or young
    company still outranks a weak partial match on an active or old one.
    """
    q = query.strip().lower()
    t = title.strip().lower()

    score = _name_similarity(q, t)

    if status == "active":
        score += 0.08
    elif status in _INACTIVE_STATUSES:
        score -= 0.08

    score += _age_bonus(date_of_creation)

    return round(max(0.0, score), 4)


def _typo_variants(text: str) -> list[str]:
    """Adjacent-letter transpositions of `text` ("Reovlut" -> "Revolut").

    Deliberately just the one typo class. Transpositions are the classic
    fast-typing slip, and they're cheap to enumerate - one variant per
    character. Every other kind of misspelling is left to the query
    suggester, which handles them more accurately than shuffling letters and
    firing a live search at each result ever could.
    """
    variants: list[str] = []
    seen = {text.lower()}

    for i in range(len(text) - 1):
        candidate = text[:i] + text[i + 1] + text[i] + text[i + 2 :]
        key = candidate.lower()
        if key not in seen and len(candidate.strip()) >= 2:
            seen.add(key)
            variants.append(candidate)

    return variants


def _to_candidate(item: dict[str, Any], query: str) -> Candidate:
    address = item.get("address", {}) or {}
    address_snippet = ", ".join(
        part
        for part in [address.get("address_line_1"), address.get("locality"), address.get("postal_code")]
        if part
    ) or None
    title = item.get("title", "")
    status = item.get("company_status", "unknown")
    date_of_creation = item.get("date_of_creation")
    return Candidate(
        company_number=item.get("company_number", ""),
        title=title,
        status=status,
        company_type=item.get("company_type", "unknown"),
        date_of_creation=date_of_creation,
        address_snippet=address_snippet,
        score=_score_candidate(query, title, status, date_of_creation),
    )


def _scored_search(
    client: CompaniesHouseClient, query: str
) -> tuple[float, str, list[Candidate]] | None:
    """Search `query` and score the hits, or None if it found nothing."""
    try:
        results = client.search_companies(query)
    except Exception:  # noqa: BLE001 - a failed retry is just a dead end
        return None
    if not results:
        return None
    candidates = [_to_candidate(item, query) for item in results]
    return max(c.score for c in candidates), query, candidates


def resolve_company(
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

    # Race the suggester against the search rather than waiting to see whether
    # it's needed. It's wanted on both outcomes - to correct a weak result, or
    # to offer an alternative to a strong-but-possibly-wrong one - and running
    # it after the fact would serialise two round-trips for no benefit.
    with ThreadPoolExecutor(max_workers=2) as pool:
        search_future = pool.submit(client.search_companies, text)
        suggestion_future = pool.submit(suggest_query, text) if suggest_query else None

        try:
            results = search_future.result()
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
            return ResolutionResult(error=f"Companies House search failed: {exc}")

        suggestion: str | None = None
        if suggestion_future is not None:
            try:
                suggestion = suggestion_future.result()
            except Exception:  # noqa: BLE001 - never let this break resolution
                # Logged, not silent: degrading to the deterministic path looks
                # identical to the suggester simply declining, so a broken or
                # misconfigured client would otherwise vanish without trace.
                _log.warning("Query suggester failed for %r", text, exc_info=True)

    # A suggestion identical to what was typed tells us nothing.
    if suggestion and suggestion.strip().lower() == text.lower():
        suggestion = None

    candidates_by_number = {c.company_number: c for c in (_to_candidate(item, text) for item in results)}
    best_score = max((c.score for c in candidates_by_number.values()), default=0.0)

    # A weak (or absent) best match might mean the query was garbled - CH's
    # search isn't fuzzy, so a typo can return nothing, or worse, a handful of
    # real-but-irrelevant matches instead. Look for a better spelling, cheapest
    # source first, and pull its results in only if they genuinely look better.
    corrected_query: str | None = None
    if best_score < _WEAK_MATCH_THRESHOLD:
        best_variant: tuple[float, str, list[Candidate]] | None = None

        for variant in _typo_variants(text)[:_MAX_TYPO_LOOKUPS]:
            scored = _scored_search(client, variant)
            if scored is None:
                continue
            if best_variant is None or scored[0] > best_variant[0]:
                best_variant = scored
            # CH returns *something* for almost any garbled string, so a
            # variant merely having results says nothing about whether it's
            # the right spelling - only a confident match is worth stopping
            # on. Anything weaker may still be beaten by a later variant.
            if scored[0] >= _WEAK_MATCH_THRESHOLD:
                break

        # Letter-shuffling only covers transpositions. If it didn't turn up
        # anything convincing, fall back to what the suggester already
        # proposed - that reaches misspellings and colloquial names which no
        # single-edit variant can produce.
        if (best_variant is None or best_variant[0] < _WEAK_MATCH_THRESHOLD) and suggestion:
            scored = _scored_search(client, suggestion.strip())
            if scored is not None and (best_variant is None or scored[0] > best_variant[0]):
                best_variant = scored

        # Only trust a correction that both beats the original *and* stands up
        # on its own - otherwise a junk query whose results scored 0.2 would
        # "correct" to different junk scoring 0.3 and be presented as a fix.
        if (
            best_variant is not None
            and best_variant[0] > best_score
            and best_variant[0] >= _WEAK_MATCH_THRESHOLD
        ):
            _, corrected_query, corrected_candidates = best_variant
            for c in corrected_candidates:
                existing = candidates_by_number.get(c.company_number)
                if existing is None or c.score > existing.score:
                    candidates_by_number[c.company_number] = c

    candidates = list(candidates_by_number.values())
    if not candidates:
        return ResolutionResult(error=f"No companies found matching '{text}'.")

    # A correction is itself a guess, so never silently auto-resolve on top of
    # one - always let the user confirm, even if only one candidate came back.
    if corrected_query is None:
        exact_active = [
            c for c in candidates if c.title.strip().lower() == text.lower() and c.status == "active"
        ]
        if len(exact_active) == 1:
            profile = client.get_company_profile(exact_active[0].company_number)
            return ResolutionResult(company_profile=profile)

    # The query matched well, but it may not have meant what it matched: "M&S"
    # fits a dozen small M&S-prefixed firms perfectly while almost every user
    # typing it means Marks and Spencer. Offer that as its own list rather
    # than ranking it against scores measured on a different query - see
    # SuggestedAlternative. Only on the confident path: when the match was
    # weak, the correction logic above has already had its turn. Computed
    # after the auto-resolve check so an exact hit doesn't pay for a search
    # whose result it would only discard.
    alternative: SuggestedAlternative | None = None
    if best_score >= _WEAK_MATCH_THRESHOLD and suggestion:
        scored = _scored_search(client, suggestion.strip())
        if scored is not None and scored[0] >= _WEAK_MATCH_THRESHOLD:
            unseen = [c for c in scored[2] if c.company_number not in candidates_by_number]
            if unseen:
                alternative = SuggestedAlternative(
                    query=suggestion.strip(),
                    candidates=sorted(unseen, key=lambda c: c.score, reverse=True),
                )

    ranked_candidates = sorted(candidates, key=lambda c: c.score, reverse=True)
    return ResolutionResult(
        candidates=ranked_candidates,
        corrected_query=corrected_query,
        suggestion=alternative,
    )
