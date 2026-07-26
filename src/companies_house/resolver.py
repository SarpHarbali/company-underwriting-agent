"""Resolve free-text user input (name or registration number) to a single company.

Identity resolution is kept deterministic and outside the LLM loop: getting the
*wrong* company silently is worse than any amount of extra friction, so ambiguous
matches are always surfaced to the user rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.companies_house.client import CompaniesHouseClient, CompanyNotFoundError

# UK company numbers: 8 digits, or 2 letters + 6 digits (e.g. SC123456, NI012345, OC123456).
_COMPANY_NUMBER_RE = re.compile(r"^(?:[A-Za-z]{2}\d{6}|\d{6,8})$")


@dataclass(frozen=True)
class Candidate:
    company_number: str
    title: str
    status: str
    company_type: str
    date_of_creation: str | None
    address_snippet: str | None


@dataclass(frozen=True)
class ResolutionResult:
    """Exactly one of `company_profile` or `candidates` is populated."""

    company_profile: dict[str, Any] | None = None
    candidates: list[Candidate] | None = None
    error: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.company_profile is not None

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.candidates)


def _looks_like_company_number(text: str) -> bool:
    return bool(_COMPANY_NUMBER_RE.match(text.strip()))


def _to_candidate(item: dict[str, Any]) -> Candidate:
    address = item.get("address", {}) or {}
    address_snippet = ", ".join(
        part
        for part in [address.get("address_line_1"), address.get("locality"), address.get("postal_code")]
        if part
    ) or None
    return Candidate(
        company_number=item.get("company_number", ""),
        title=item.get("title", ""),
        status=item.get("company_status", "unknown"),
        company_type=item.get("company_type", "unknown"),
        date_of_creation=item.get("date_of_creation"),
        address_snippet=address_snippet,
    )


def resolve_company(client: CompaniesHouseClient, user_input: str) -> ResolutionResult:
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
        results = client.search_companies(text)
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
        return ResolutionResult(error=f"Companies House search failed: {exc}")

    if not results:
        return ResolutionResult(error=f"No companies found matching '{text}'.")

    candidates = [_to_candidate(item) for item in results]

    exact_active = [
        c for c in candidates if c.title.strip().lower() == text.lower() and c.status == "active"
    ]
    if len(exact_active) == 1:
        profile = client.get_company_profile(exact_active[0].company_number)
        return ResolutionResult(company_profile=profile)

    return ResolutionResult(candidates=candidates)
