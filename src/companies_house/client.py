"""Thin wrapper around the Companies House public data REST API.

Docs: https://developer-specs.company-information.service.gov.uk/
Auth: HTTP Basic, API key as username, blank password.
"""

from __future__ import annotations

from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class CompaniesHouseAPIError(Exception):
    """Raised for unexpected (non-404) Companies House API failures."""


class CompanyNotFoundError(Exception):
    """Raised when a specific company/resource genuinely doesn't exist (404)."""


class RateLimitedError(CompaniesHouseAPIError):
    """Raised when retries are exhausted while rate-limited (429)."""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, requests.exceptions.RequestException) and not isinstance(
        exc, (CompanyNotFoundError,)
    )


class CompaniesHouseClient:
    """Minimal, resilient client for the endpoints this project needs."""

    def __init__(self, api_key: str, base_url: str, public_base_url: str, timeout: float = 15.0):
        self._session = requests.Session()
        self._session.auth = (api_key, "")
        self._base_url = base_url.rstrip("/")
        self.public_base_url = public_base_url.rstrip("/")
        self._timeout = timeout

    def public_company_url(self, company_number: str) -> str:
        return f"{self.public_base_url}/company/{company_number}"

    @retry(
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        url = f"{self._base_url}{path}"
        response = self._session.get(url, params=params, timeout=self._timeout)

        if response.status_code == 404:
            raise CompanyNotFoundError(f"Not found: {path}")
        if response.status_code == 429:
            # Let tenacity retry with backoff; if attempts are exhausted the
            # RequestException propagates and we re-raise as RateLimitedError below.
            raise requests.exceptions.RequestException(f"Rate limited (429) on {path}")
        if response.status_code >= 500:
            raise requests.exceptions.RequestException(
                f"Companies House server error {response.status_code} on {path}"
            )
        if response.status_code >= 400:
            raise CompaniesHouseAPIError(
                f"Companies House API error {response.status_code} on {path}: {response.text[:300]}"
            )

        if not response.content:
            return None
        return response.json()

    def _get_safe(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        try:
            return self._get(path, params)
        except requests.exceptions.RequestException as exc:
            raise RateLimitedError(f"Companies House request failed after retries: {exc}") from exc

    def search_companies(self, query: str, items_per_page: int = 20) -> list[dict[str, Any]]:
        data = self._get_safe(
            "/search/companies", params={"q": query, "items_per_page": items_per_page}
        )
        return (data or {}).get("items", [])

    def get_company_profile(self, company_number: str) -> dict[str, Any]:
        data = self._get_safe(f"/company/{company_number}")
        if data is None:
            raise CompanyNotFoundError(f"No profile data for {company_number}")
        return data

    def get_officers(self, company_number: str) -> list[dict[str, Any]]:
        try:
            data = self._get_safe(f"/company/{company_number}/officers")
        except CompanyNotFoundError:
            return []
        return (data or {}).get("items", [])

    def get_filing_history(self, company_number: str, items_per_page: int = 25) -> list[dict[str, Any]]:
        try:
            data = self._get_safe(
                f"/company/{company_number}/filing-history",
                params={"items_per_page": items_per_page},
            )
        except CompanyNotFoundError:
            return []
        return (data or {}).get("items", [])

    def get_persons_with_significant_control(self, company_number: str) -> list[dict[str, Any]]:
        try:
            data = self._get_safe(f"/company/{company_number}/persons-with-significant-control")
        except CompanyNotFoundError:
            return []
        return (data or {}).get("items", [])

    def get_charges(self, company_number: str) -> list[dict[str, Any]]:
        try:
            data = self._get_safe(f"/company/{company_number}/charges")
        except CompanyNotFoundError:
            return []
        return (data or {}).get("items", [])

    def get_insolvency(self, company_number: str) -> dict[str, Any] | None:
        try:
            return self._get_safe(f"/company/{company_number}/insolvency")
        except CompanyNotFoundError:
            return None
