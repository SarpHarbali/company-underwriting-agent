from src.companies_house.client import CompanyNotFoundError
from src.companies_house.resolver import resolve_company


class FakeClient:
    def __init__(self, profiles=None, search_results=None, raise_not_found=False):
        self._profiles = profiles or {}
        self._search_results = search_results if search_results is not None else []
        self._raise_not_found = raise_not_found

    def get_company_profile(self, company_number):
        if self._raise_not_found or company_number not in self._profiles:
            raise CompanyNotFoundError(company_number)
        return self._profiles[company_number]

    def search_companies(self, query):
        return self._search_results


def test_resolves_directly_by_company_number():
    client = FakeClient(profiles={"09446231": {"company_name": "Monzo Bank Limited", "company_number": "09446231"}})
    result = resolve_company(client, "09446231")
    assert result.is_resolved
    assert result.company_profile["company_name"] == "Monzo Bank Limited"


def test_number_not_found_gives_clean_error():
    client = FakeClient(raise_not_found=True)
    result = resolve_company(client, "99999999")
    assert not result.is_resolved
    assert "No company found" in result.error


def test_empty_input_is_an_error():
    client = FakeClient()
    result = resolve_company(client, "   ")
    assert result.error is not None


def test_single_exact_active_match_auto_resolves():
    client = FakeClient(
        profiles={"12345678": {"company_name": "Acme Ltd", "company_number": "12345678"}},
        search_results=[
            {
                "title": "Acme Ltd",
                "company_number": "12345678",
                "company_status": "active",
                "company_type": "ltd",
            }
        ],
    )
    result = resolve_company(client, "Acme Ltd")
    assert result.is_resolved
    assert result.company_profile["company_number"] == "12345678"


def test_multiple_matches_are_surfaced_as_candidates():
    client = FakeClient(
        search_results=[
            {"title": "Acme Ltd", "company_number": "111", "company_status": "active", "company_type": "ltd"},
            {"title": "Acme Group Ltd", "company_number": "222", "company_status": "active", "company_type": "ltd"},
        ]
    )
    result = resolve_company(client, "Acme")
    assert result.is_ambiguous
    assert len(result.candidates) == 2
    assert {c.company_number for c in result.candidates} == {"111", "222"}


def test_no_search_results_is_an_error():
    client = FakeClient(search_results=[])
    result = resolve_company(client, "Nonexistent Company")
    assert result.error is not None
    assert "No companies found" in result.error
