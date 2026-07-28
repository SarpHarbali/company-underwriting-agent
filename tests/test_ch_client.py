import time

import pytest

from src.companies_house.client import (
    CompaniesHouseAPIError,
    CompaniesHouseClient,
    CompanyNotFoundError,
    RateLimitedError,
)


class FakeResponse:
    def __init__(self, status_code: int, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.content = b"x" if json_data is not None else b""

    def json(self):
        return self._json_data


def make_client(get_fn) -> CompaniesHouseClient:
    client = CompaniesHouseClient(
        api_key="fake-key",
        base_url="https://api.example.test",
        public_base_url="https://public.example.test",
    )
    client._session.get = get_fn  # type: ignore[method-assign]
    return client


def test_get_company_profile_success():
    def get_fn(url, params=None, timeout=None):
        assert url == "https://api.example.test/company/12345678"
        return FakeResponse(200, {"company_name": "Acme Ltd", "company_number": "12345678"})

    client = make_client(get_fn)
    profile = client.get_company_profile("12345678")
    assert profile["company_name"] == "Acme Ltd"


def test_get_company_profile_not_found():
    def get_fn(url, params=None, timeout=None):
        return FakeResponse(404)

    client = make_client(get_fn)
    with pytest.raises(CompanyNotFoundError):
        client.get_company_profile("00000000")


def test_rate_limit_exhausts_retries_and_raises(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    calls = {"count": 0}

    def get_fn(url, params=None, timeout=None):
        calls["count"] += 1
        return FakeResponse(429)

    client = make_client(get_fn)
    with pytest.raises(RateLimitedError):
        client.get_company_profile("12345678")
    assert calls["count"] == 4  # stop_after_attempt(4)


def test_other_client_error_raises_api_error():
    def get_fn(url, params=None, timeout=None):
        return FakeResponse(400, text="bad request")

    client = make_client(get_fn)
    with pytest.raises(CompaniesHouseAPIError):
        client.get_company_profile("12345678")


def test_officers_returns_empty_list_when_not_found():
    def get_fn(url, params=None, timeout=None):
        return FakeResponse(404)

    client = make_client(get_fn)
    assert client.get_officers("12345678") == []


def test_filing_history_returns_items():
    def get_fn(url, params=None, timeout=None):
        assert url.endswith("/company/12345678/filing-history")
        assert params == {"items_per_page": 10}
        return FakeResponse(200, {"items": [{"type": "AA", "date": "2025-01-01"}]})

    filings = make_client(get_fn).get_filing_history("12345678", items_per_page=10)

    assert filings == [{"type": "AA", "date": "2025-01-01"}]
