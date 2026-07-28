from datetime import date

from src.companies_house.client import CompanyNotFoundError
from src.companies_house.name_index import NameMatch
from src.companies_house.names import normalised_forms
from src.companies_house.resolver import resolve_company, suggest_alternative


def match(
    name,
    number,
    status="active",
    incorporated=None,
    previous_of=None,
    postcode=None,
):
    return NameMatch(
        company_number=number,
        current_name=previous_of or name,
        matched_name=name,
        is_previous_name=previous_of is not None,
        company_status=status,
        incorporation_date=date(incorporated, 1, 1) if incorporated else None,
        postcode=postcode,
    )


class FakeRepository:
    def __init__(self, results=None, results_by_query=None, error=None):
        self._results = results or []
        self._results_by_query = results_by_query or {}
        self._error = error
        self.queries = []

    def find_candidates(self, query_norm, query_stem, variants):
        if self._error:
            raise self._error
        self.queries.append(query_norm)
        return self._results_by_query.get(query_norm, self._results)


class FakeClient:
    def __init__(self, profiles=None):
        self._profiles = profiles or {}
        self.calls = []

    def get_company_profile(self, company_number):
        self.calls.append(company_number)
        if company_number not in self._profiles:
            raise CompanyNotFoundError(company_number)
        return self._profiles[company_number]


def resolve(repository, text, client=None, **kwargs):
    return resolve_company(repository, client or FakeClient(), text, **kwargs)


def test_company_number_resolves_without_querying_name_index():
    repository = FakeRepository()
    client = FakeClient(
        profiles={
            "09446231": {
                "company_name": "Monzo Bank Limited",
                "company_number": "09446231",
            }
        }
    )

    result = resolve(repository, "09446231", client)

    assert result.company_profile["company_name"] == "Monzo Bank Limited"
    assert repository.queries == []
    assert client.calls == ["09446231"]


def test_unknown_company_number_returns_a_clear_error():
    result = resolve(FakeRepository(), "99999999")
    assert not result.is_resolved
    assert "No company found" in result.error


def test_empty_or_unmatched_name_returns_a_clear_error():
    assert resolve(FakeRepository(), "   ").error
    assert "No companies found" in resolve(
        FakeRepository(), "Nonexistent Company"
    ).error


def test_name_search_returns_candidates_without_fetching_a_profile():
    repository = FakeRepository(
        results=[
            match(
                "Acme Ltd",
                "111",
                previous_of="Zenith Holdings Ltd",
            ),
            match("Acme Group Ltd", "222"),
        ]
    )
    client = FakeClient()

    result = resolve(repository, "Acme", client)

    assert result.is_ambiguous
    assert not result.is_resolved
    assert client.calls == []
    former_name_match = next(
        candidate
        for candidate in result.candidates
        if candidate.company_number == "111"
    )
    assert former_name_match.title == "Zenith Holdings Ltd"
    assert former_name_match.matched_name == "Acme Ltd"


def test_candidate_list_is_deduplicated_and_capped():
    rows = [match("Acme Ltd", "0"), match("Acme Trading Ltd", "0")]
    rows.extend(match(f"Acme {index} Ltd", str(index)) for index in range(1, 20))

    result = resolve(FakeRepository(results=rows), "Acme")
    numbers = [candidate.company_number for candidate in result.candidates]

    assert len(numbers) == 10
    assert len(numbers) == len(set(numbers))


def test_weak_query_uses_a_confident_correction():
    repository = FakeRepository(
        results_by_query={
            "wetherspoon": [
                match("J D WETHERSPOON PLC", "01709784", incorporated=1983)
            ]
        }
    )

    result = resolve(
        repository,
        "spoons",
        suggest_query=lambda _: "wetherspoon",
    )

    assert result.corrected_query == "wetherspoon"
    assert result.candidates[0].company_number == "01709784"
    assert not result.can_suggest


def test_correction_must_be_strong_and_augments_direct_matches():
    weak_repository = FakeRepository(
        results=[match("Totally Unrelated Holdings", "999")],
        results_by_query={
            "qqzzx trading": [match("Slightly Less Unrelated Group", "888")]
        },
    )
    weak_result = resolve(
        weak_repository,
        "qqzzx tradng",
        suggest_query=lambda _: "qqzzx trading",
    )
    assert weak_result.corrected_query is None
    assert [candidate.company_number for candidate in weak_result.candidates] == [
        "999"
    ]

    augmented_repository = FakeRepository(
        results=[match("Zebra Unrelated Holdings", "999")],
        results_by_query={"revolut": [match("Revolut Ltd", "111")]},
    )
    augmented_result = resolve(
        augmented_repository,
        "rvlut",
        suggest_query=lambda _: "revolut",
    )
    assert {candidate.company_number for candidate in augmented_result.candidates} == {
        "111",
        "999",
    }
    assert augmented_result.candidates[0].company_number == "111"


def test_strong_match_defers_the_suggester_and_queries_once():
    calls = []

    def recording_suggester(text):
        calls.append(text)
        return "acme trading"

    repository = FakeRepository(results=[match("Acme Ltd", "111")])
    result = resolve(
        repository,
        "Acme Ltd",
        suggest_query=recording_suggester,
    )

    assert calls == []
    assert result.can_suggest
    assert repository.queries == [normalised_forms("Acme Ltd")[0]]


def test_suggester_failure_does_not_break_name_resolution():
    def unavailable(_text):
        raise RuntimeError("LLM unavailable")

    result = resolve(
        FakeRepository(),
        "Nonexistent Qqzzx",
        suggest_query=unavailable,
    )

    assert "No companies found" in result.error


def test_name_index_failure_is_surfaced():
    result = resolve(
        FakeRepository(error=RuntimeError("connection refused")),
        "Acme Ltd",
    )
    assert "connection refused" in result.error


def test_requested_alternative_returns_only_unseen_strong_matches():
    repository = FakeRepository(
        results_by_query={
            "marks and spencer": [
                match("M&S ASSET MANAGEMENT LIMITED", "08432196"),
                match(
                    "MARKS AND SPENCER P.L.C.",
                    "00214436",
                    incorporated=1926,
                ),
            ]
        }
    )

    alternative = suggest_alternative(
        repository,
        "M&S",
        lambda _: "marks and spencer",
        listed_numbers=["08432196"],
    )

    assert alternative.query == "marks and spencer"
    assert [candidate.company_number for candidate in alternative.candidates] == [
        "00214436"
    ]


def test_requested_alternative_rejects_weak_or_duplicate_results():
    weak_repository = FakeRepository(
        results_by_query={
            "something unrelated": [match("Totally Different Holdings", "222")]
        }
    )
    assert (
        suggest_alternative(
            weak_repository,
            "Acme",
            lambda _: "something unrelated",
        )
        is None
    )

    duplicate_repository = FakeRepository(
        results=[match("Acme Trading Ltd", "111")]
    )
    assert (
        suggest_alternative(
            duplicate_repository,
            "Acme Trading Co",
            lambda _: "acme trading",
            listed_numbers=["111"],
        )
        is None
    )
