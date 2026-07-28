from datetime import date

from src.companies_house.client import CompanyNotFoundError
from src.companies_house.name_index import NameMatch
from src.companies_house.names import normalised_forms
from src.companies_house.resolver import resolve_company, suggest_alternative


def match(name, number, status="active", incorporated=None, previous_of=None, postcode=None):
    """One index row. `previous_of` marks it as a former name of that company."""
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
    """Stands in for Postgres by matching queries against a fixed row set.

    Retrieval fidelity isn't the point here - `results_by_query` lets a test
    say "this query retrieves these rows" directly. What is under test is what
    the resolver does with a shortlist, not how the shortlist was fetched.
    """

    def __init__(self, results=None, results_by_query=None):
        self._results = results if results is not None else []
        self._results_by_query = results_by_query or {}
        self.queries = []

    def find_candidates(self, query_norm, query_stem, variants):
        self.queries.append(query_norm)
        if query_norm in self._results_by_query:
            return self._results_by_query[query_norm]
        return self._results


class FakeClient:
    def __init__(self, profiles=None, raise_not_found=False):
        self._profiles = profiles or {}
        self._raise_not_found = raise_not_found

    def get_company_profile(self, company_number):
        if self._raise_not_found or company_number not in self._profiles:
            raise CompanyNotFoundError(company_number)
        return self._profiles[company_number]


def resolve(repository, client=None, text="", **kwargs):
    return resolve_company(repository, client or FakeClient(), text, **kwargs)


def test_resolves_directly_by_company_number():
    client = FakeClient(profiles={"09446231": {"company_name": "Monzo Bank Limited", "company_number": "09446231"}})
    result = resolve(FakeRepository(), client, "09446231")
    assert result.is_resolved
    assert result.company_profile["company_name"] == "Monzo Bank Limited"


def test_number_lookup_never_touches_the_index():
    # The register is the source of truth for a company that's already been
    # identified - the local snapshot has nothing to add.
    repository = FakeRepository()
    client = FakeClient(profiles={"09446231": {"company_number": "09446231"}})
    resolve(repository, client, "09446231")
    assert repository.queries == []


def test_number_not_found_gives_clean_error():
    result = resolve(FakeRepository(), FakeClient(raise_not_found=True), "99999999")
    assert not result.is_resolved
    assert "No company found" in result.error


def test_empty_input_is_an_error():
    result = resolve(FakeRepository(), text="   ")
    assert result.error is not None


def test_a_single_exact_active_match_is_still_only_a_candidate():
    # The strongest possible name evidence, and it still doesn't pick for you:
    # a wrong company accepted silently reads exactly like a right one.
    client = FakeClient(profiles={"12345678": {"company_name": "Acme Ltd", "company_number": "12345678"}})
    repository = FakeRepository(results=[match("Acme Ltd", "12345678")])
    result = resolve(repository, client, "Acme Ltd")
    assert result.is_ambiguous
    assert not result.is_resolved
    assert [c.company_number for c in result.candidates] == ["12345678"]


def test_a_name_search_never_fetches_a_profile_on_its_own():
    # The profile call belongs to the user's Select click, not to resolution.
    class ExplodingClient:
        def get_company_profile(self, company_number):
            raise AssertionError("resolution must not fetch a profile by name")

    repository = FakeRepository(results=[match("Acme Ltd", "111")])
    assert resolve(repository, ExplodingClient(), "Acme Ltd").is_ambiguous


def test_a_confident_match_still_offers_the_suggestion_question():
    # Being sure of the top match is not being sure it's what was meant.
    repository = FakeRepository(results=[match("Acme Ltd", "111")])
    assert resolve(repository, text="Acme Ltd", suggest_query=lambda _: "acme").can_suggest


def test_exact_match_on_a_former_name_is_shown_under_its_current_name():
    # A former name is a legitimate way to *find* a company, but the company
    # isn't called that any more - the user must confirm the swap.
    repository = FakeRepository(results=[match("Acme Ltd", "111", previous_of="Zenith Holdings Ltd")])
    result = resolve(repository, text="Acme Ltd")
    assert result.is_ambiguous
    assert result.candidates[0].title == "Zenith Holdings Ltd"
    assert result.candidates[0].matched_name == "Acme Ltd"


def test_multiple_matches_are_surfaced_as_candidates():
    repository = FakeRepository(results=[match("Acme Ltd", "111"), match("Acme Group Ltd", "222")])
    result = resolve(repository, text="Acme")
    assert result.is_ambiguous
    assert {c.company_number for c in result.candidates} == {"111", "222"}


def test_no_results_is_an_error():
    result = resolve(FakeRepository(results=[]), text="Nonexistent Company")
    assert result.error is not None
    assert "No companies found" in result.error


def test_a_company_matched_by_several_names_appears_once():
    repository = FakeRepository(
        results=[
            match("Acme Trading Ltd", "111"),
            match("Acme Trading Company Ltd", "111", previous_of="Acme Trading Ltd"),
        ]
    )
    result = resolve(repository, text="Acme Trading")
    assert [c.company_number for c in result.candidates] == ["111"]
    # The better-scoring of its two names is the one kept.
    assert result.candidates[0].is_previous_name is False


def test_candidates_are_ranked_best_match_first():
    repository = FakeRepository(
        results=[
            match("Zebra Unrelated Holdings", "999"),
            match("Acme Ltd", "111"),
            match("Acme Ltd", "222"),
        ]
    )
    result = resolve(repository, text="Acme Ltd")
    numbers_in_order = [c.company_number for c in result.candidates]
    assert set(numbers_in_order[:2]) == {"111", "222"}
    assert numbers_in_order[2] == "999"
    assert result.candidates[0].score > result.candidates[2].score


def test_dissolved_exact_match_outranks_weak_active_match():
    repository = FakeRepository(
        results=[
            match("Acme Ltd", "111", status="dissolved"),
            match("Acme Consulting Group International", "222"),
        ]
    )
    result = resolve(repository, text="Acme Ltd")
    assert [c.company_number for c in result.candidates] == ["111", "222"]


def test_older_company_ranked_first_among_equally_similar_names():
    repository = FakeRepository(
        results=[match("Acme Ltd", "111", incorporated=2020), match("Acme Ltd", "222", incorporated=1930)]
    )
    result = resolve(repository, text="Acme Ltd")
    assert [c.company_number for c in result.candidates] == ["222", "111"]
    assert result.candidates[0].score > result.candidates[1].score


def test_current_name_beats_an_identical_former_name():
    # One company is called this today, another used to be. Both are offered;
    # the current holder of the name is simply the likelier reading, so it
    # ranks first.
    repository = FakeRepository(
        results=[
            match("Acme Ltd", "111", previous_of="Zenith Ltd"),
            match("Acme Ltd", "222"),
        ]
    )
    result = resolve(repository, FakeClient(profiles={"222": {}}), "Acme Ltd")
    assert result.is_ambiguous
    assert [c.company_number for c in result.candidates] == ["222", "111"]


def test_longer_title_is_not_penalised_for_its_length():
    # Both titles contain the query as a leading token, so neither should win
    # on name alone - the older company takes it. Scoring whole strings against
    # each other would rank the shorter title first purely because it's shorter.
    repository = FakeRepository(
        results=[
            match("MONZO TYRES LTD", "17125525", incorporated=2026),
            match("MONZO BANK LIMITED", "09446231", incorporated=2015),
        ]
    )
    result = resolve(repository, text="monzo")
    assert [c.company_number for c in result.candidates] == ["09446231", "17125525"]


def test_name_matches_a_token_run_that_is_not_at_the_start():
    # People search the name they know, not the registered one - nobody types
    # the "J D". An active, long-established J D Wetherspoon must beat a
    # dissolved company whose name merely *begins* with "Wetherspoon".
    repository = FakeRepository(
        results=[
            match("WETHERSPOON & PARTNER LIMITED", "07107915", status="dissolved", incorporated=2009),
            match("J D WETHERSPOON PLC", "01709784", incorporated=1983),
        ]
    )
    result = resolve(repository, text="Wetherspoon")
    assert [c.company_number for c in result.candidates] == ["01709784", "07107915"]


def test_leading_token_match_still_edges_out_a_non_leading_one():
    # All else equal, a title that starts with the query is the marginally
    # better match - the non-leading discount must not erase that.
    repository = FakeRepository(
        results=[match("J D Wetherspoon plc", "222"), match("Wetherspoon Ltd", "111")]
    )
    result = resolve(repository, text="Wetherspoon")
    assert [c.company_number for c in result.candidates] == ["111", "222"]


def test_candidate_list_is_capped_for_disambiguation():
    # Retrieval fetches hundreds on purpose; handing the user hundreds would
    # just move the disambiguation problem rather than solve it.
    repository = FakeRepository(results=[match(f"Acme {i} Ltd", str(i)) for i in range(50)])
    result = resolve(repository, text="Acme")
    assert len(result.candidates) == 10


def test_suggester_fixes_a_typo_the_index_cannot_reach():
    repository = FakeRepository(
        results=[],
        results_by_query={
            "saint laurent": [match("YVES SAINT LAURENT UK LIMITED", "111")],
        },
    )
    result = resolve(repository, text="saint laurant", suggest_query=lambda _: "saint laurent")
    assert result.corrected_query == "saint laurent"
    assert result.candidates[0].company_number == "111"


def test_suggester_resolves_a_colloquial_name():
    # The real motivation: no edit-distance variant turns "spoons" into
    # "wetherspoon", so retrieval alone can never find it.
    repository = FakeRepository(
        results=[],
        results_by_query={
            "wetherspoon": [match("J D WETHERSPOON PLC", "01709784", incorporated=1983)],
        },
    )
    result = resolve(repository, text="spoons", suggest_query=lambda _: "wetherspoon")
    assert result.corrected_query == "wetherspoon"
    assert result.candidates[0].company_number == "01709784"


def test_a_correction_is_labelled_as_one():
    # The user searched for something they didn't get results for; the list in
    # front of them answers a question they didn't ask, so it must say so.
    repository = FakeRepository(
        results=[],
        results_by_query={"acme ltd": [match("Acme Ltd", "111")]},
    )
    result = resolve(repository, text="acme limted", suggest_query=lambda _: "acme ltd")
    assert result.is_ambiguous
    assert result.corrected_query == "acme ltd"


def test_weak_original_match_is_augmented_not_replaced():
    # The original query isn't empty - it happens to also match an unrelated,
    # weakly-similar company - but the real target only turns up under the
    # corrected spelling. Both end up listed, genuine match first.
    repository = FakeRepository(
        results=[match("Zebra Unrelated Holdings", "999")],
        results_by_query={"revolut": [match("Revolut Ltd", "111")]},
    )
    result = resolve(repository, text="rvlut", suggest_query=lambda _: "revolut")
    assert result.corrected_query == "revolut"
    assert {c.company_number for c in result.candidates} == {"111", "999"}
    assert result.candidates[0].company_number == "111"


def test_a_correction_must_be_confident_not_merely_better():
    # Junk correcting to slightly-less-bad junk is not a correction. Neither
    # should be presented as "showing results for ...".
    repository = FakeRepository(
        results=[match("Totally Unrelated Holdings", "999")],
        results_by_query={"qqzzx trading": [match("Slightly Less Unrelated Group", "888")]},
    )
    result = resolve(repository, text="qqzzx tradng", suggest_query=lambda _: "qqzzx trading")
    assert result.corrected_query is None
    assert [c.company_number for c in result.candidates] == ["999"]


def test_confident_match_does_not_call_the_suggester():
    # The whole point of deferring: a query that found good matches spends no
    # LLM call unless the user says the matches are wrong.
    calls = []

    def recording_suggester(text):
        calls.append(text)
        return "marks and spencer"

    repository = FakeRepository(
        results=[match("M&S ASSET MANAGEMENT LIMITED", "08432196", incorporated=2013)]
    )
    result = resolve(repository, text="M&S", suggest_query=recording_suggester)
    assert calls == []
    assert result.can_suggest
    assert [c.company_number for c in result.candidates] == ["08432196"]


def test_a_corrected_query_is_not_offered_a_second_opinion():
    # The suggester has already spoken for this query; asking again would only
    # spend another call to hear the same answer.
    repository = FakeRepository(
        results=[],
        results_by_query={"wetherspoon": [match("J D WETHERSPOON PLC", "01709784", incorporated=1983)]},
    )
    result = resolve(repository, text="spoons", suggest_query=lambda _: "wetherspoon")
    assert result.corrected_query == "wetherspoon"
    assert not result.can_suggest


def test_no_suggester_means_nothing_to_offer():
    repository = FakeRepository(results=[match("Acme Ltd", "111"), match("Acme Group Ltd", "222")])
    assert not resolve(repository, text="Acme").can_suggest


def test_alternative_is_found_on_request():
    # "M&S" matches small M&S-prefixed firms perfectly well, so no correction
    # fires - but Marks and Spencer is there for the asking.
    repository = FakeRepository(
        results_by_query={
            "marks and spencer": [match("MARKS AND SPENCER P.L.C.", "00214436", incorporated=1926)],
        },
    )
    alternative = suggest_alternative(
        repository, "M&S", lambda _: "marks and spencer", listed_numbers=["08432196"]
    )
    assert alternative is not None
    assert alternative.query == "marks and spencer"
    assert [c.company_number for c in alternative.candidates] == ["00214436"]


def test_no_alternative_when_the_suggestion_is_the_query():
    repository = FakeRepository(results=[match("Monzo Bank Limited", "09446231")])
    assert suggest_alternative(repository, "monzo", lambda _: "Monzo") is None


def test_no_alternative_when_the_suggester_declines():
    repository = FakeRepository(results=[match("Acme Ltd", "111")])
    assert suggest_alternative(repository, "qqzzx", lambda _: None) is None


def test_no_alternative_when_its_results_are_weak():
    repository = FakeRepository(
        results_by_query={"something unrelated": [match("Totally Different Holdings", "222")]},
    )
    assert suggest_alternative(repository, "Acme", lambda _: "something unrelated") is None


def test_alternative_excludes_companies_already_listed():
    repository = FakeRepository(results=[match("Acme Trading Ltd", "111")])
    alternative = suggest_alternative(
        repository, "Acme Trading Co", lambda _: "acme trading", listed_numbers=["111"]
    )
    assert alternative is None


def test_alternative_survives_a_broken_suggester():
    # An outage costs the suggestion, never the results already on screen.
    def exploding(text):
        raise RuntimeError("LLM unavailable")

    repository = FakeRepository(results=[match("Acme Ltd", "111")])
    assert suggest_alternative(repository, "Acme", exploding) is None


def test_suggester_declining_leaves_a_clean_error():
    result = resolve(FakeRepository(results=[]), text="Nonexistent Qqzzx", suggest_query=lambda _: None)
    assert result.error is not None
    assert result.corrected_query is None


def test_suggester_failure_does_not_break_resolution():
    # A missing key or an outage must degrade to the deterministic path.
    def exploding(text):
        raise RuntimeError("LLM unavailable")

    result = resolve(FakeRepository(results=[]), text="Nonexistent Qqzzx", suggest_query=exploding)
    assert result.error is not None
    assert "No companies found" in result.error


def test_index_failure_is_surfaced_not_swallowed():
    class BrokenRepository:
        def find_candidates(self, *_):
            raise RuntimeError("connection refused")

    result = resolve(BrokenRepository(), text="Acme Ltd")
    assert result.error is not None
    assert "connection refused" in result.error


def test_no_lookup_for_a_confident_query_beyond_the_first():
    # A strong match is answered by one index query and nothing else - no
    # correction lookup, and no speculative lookup for a suggestion nobody
    # asked for.
    repository = FakeRepository(results=[match("Acme Ltd", "111")])
    result = resolve(repository, text="Acme Ltd", suggest_query=lambda _: "acme trading")
    assert result.is_ambiguous
    assert repository.queries == [normalised_forms("Acme Ltd")[0]]
