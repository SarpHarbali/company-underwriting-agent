from src.companies_house.client import CompanyNotFoundError
from src.companies_house.resolver import resolve_company


class FakeClient:
    def __init__(self, profiles=None, search_results=None, search_results_by_query=None, raise_not_found=False):
        self._profiles = profiles or {}
        self._search_results = search_results if search_results is not None else []
        self._search_results_by_query = search_results_by_query or {}
        self._raise_not_found = raise_not_found
        self.search_queries = []

    def get_company_profile(self, company_number):
        if self._raise_not_found or company_number not in self._profiles:
            raise CompanyNotFoundError(company_number)
        return self._profiles[company_number]

    def search_companies(self, query):
        self.search_queries.append(query)
        if query in self._search_results_by_query:
            return self._search_results_by_query[query]
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


def test_candidates_are_ranked_best_match_first():
    client = FakeClient(
        search_results=[
            {"title": "Zebra Unrelated Holdings", "company_number": "999", "company_status": "active", "company_type": "ltd"},
            {"title": "Acme Ltd", "company_number": "111", "company_status": "active", "company_type": "ltd"},
            {"title": "Acme Ltd", "company_number": "222", "company_status": "active", "company_type": "ltd"},
        ]
    )
    result = resolve_company(client, "Acme Ltd")
    assert result.is_ambiguous
    numbers_in_order = [c.company_number for c in result.candidates]
    assert set(numbers_in_order[:2]) == {"111", "222"}
    assert numbers_in_order[2] == "999"
    assert result.candidates[0].score > result.candidates[2].score


def test_dissolved_exact_match_outranks_weak_active_match():
    client = FakeClient(
        search_results=[
            {"title": "Acme Ltd", "company_number": "111", "company_status": "dissolved", "company_type": "ltd"},
            {
                "title": "Acme Consulting Group International",
                "company_number": "222",
                "company_status": "active",
                "company_type": "ltd",
            },
        ]
    )
    result = resolve_company(client, "Acme Ltd")
    assert result.is_ambiguous
    assert [c.company_number for c in result.candidates] == ["111", "222"]


def test_typo_is_corrected_via_transposition_retry():
    # "reovlut" is "revolut" with the 3rd/4th letters swapped - a classic
    # adjacent-transposition typo. The direct query returns nothing; the
    # transposition-variant retry should find it.
    client = FakeClient(
        search_results=[],
        search_results_by_query={
            "revolut": [
                {"title": "Revolut Ltd", "company_number": "111", "company_status": "active", "company_type": "ltd"},
            ]
        },
    )
    result = resolve_company(client, "reovlut")
    assert result.is_ambiguous
    assert result.corrected_query == "revolut"
    assert result.candidates[0].company_number == "111"


def test_typo_correction_never_auto_resolves_even_with_one_candidate():
    # Even a single, exact, active match found via a corrected spelling must
    # still be surfaced for confirmation rather than silently auto-resolved -
    # the correction itself is a guess.
    client = FakeClient(
        search_results=[],
        search_results_by_query={
            "acme ltd": [
                {"title": "Acme Ltd", "company_number": "111", "company_status": "active", "company_type": "ltd"},
            ]
        },
    )
    result = resolve_company(client, "acme ldt")
    assert result.is_ambiguous
    assert not result.is_resolved
    assert result.corrected_query == "acme ltd"


def test_no_typo_variant_matches_gives_clean_error():
    client = FakeClient(search_results=[])
    result = resolve_company(client, "Nonexistent Company")
    assert result.error is not None
    assert "No companies found" in result.error
    assert result.corrected_query is None


def test_weak_original_match_is_augmented_by_a_better_variant():
    # The original query isn't empty - it happens to also match an unrelated,
    # weakly-similar company - but the real target only turns up under the
    # corrected spelling. Both should end up in the list, with the genuine
    # match ranked first.
    client = FakeClient(
        search_results=[],
        search_results_by_query={
            "reovlut": [
                {"title": "Zebra Unrelated Holdings", "company_number": "999", "company_status": "active", "company_type": "ltd"},
            ],
            "revolut": [
                {"title": "Revolut Ltd", "company_number": "111", "company_status": "active", "company_type": "ltd"},
            ],
        },
    )
    result = resolve_company(client, "reovlut")
    assert result.is_ambiguous
    assert result.corrected_query == "revolut"
    numbers = {c.company_number for c in result.candidates}
    assert numbers == {"111", "999"}
    assert result.candidates[0].company_number == "111"


def test_older_company_ranked_first_among_equally_similar_names():
    client = FakeClient(
        search_results=[
            {
                "title": "Acme Ltd",
                "company_number": "111",
                "company_status": "active",
                "company_type": "ltd",
                "date_of_creation": "2020-01-01",
            },
            {
                "title": "Acme Ltd",
                "company_number": "222",
                "company_status": "active",
                "company_type": "ltd",
                "date_of_creation": "1930-01-01",
            },
        ]
    )
    result = resolve_company(client, "Acme Ltd")
    assert result.is_ambiguous
    assert [c.company_number for c in result.candidates] == ["222", "111"]
    assert result.candidates[0].score > result.candidates[1].score


def test_longer_title_is_not_penalised_for_its_length():
    # Both titles match the query's leading token exactly, so neither should
    # win on name alone - the older company takes it. Scoring whole strings
    # against each other used to rank the shorter title first purely because
    # it was shorter.
    client = FakeClient(
        search_results=[
            {
                "title": "MONZO TYRES LTD",
                "company_number": "17125525",
                "company_status": "active",
                "company_type": "ltd",
                "date_of_creation": "2026-03-30",
            },
            {
                "title": "MONZO BANK LIMITED",
                "company_number": "09446231",
                "company_status": "active",
                "company_type": "ltd",
                "date_of_creation": "2015-02-18",
            },
        ]
    )
    result = resolve_company(client, "monzo")
    assert result.is_ambiguous
    assert [c.company_number for c in result.candidates] == ["09446231", "17125525"]


def test_name_matches_a_token_run_that_is_not_at_the_start():
    # People search the name they know, not the registered one - nobody types
    # the "J D". An active, long-established J D Wetherspoon must beat two
    # dissolved companies whose names merely *begin* with "Wetherspoon".
    client = FakeClient(
        search_results=[
            {
                "title": "WETHERSPOON & PARTNER LIMITED",
                "company_number": "07107915",
                "company_status": "dissolved",
                "company_type": "ltd",
                "date_of_creation": "2009-12-10",
            },
            {
                "title": "J D WETHERSPOON PLC",
                "company_number": "01709784",
                "company_status": "active",
                "company_type": "plc",
                "date_of_creation": "1983-03-25",
            },
        ]
    )
    result = resolve_company(client, "Wetherspoon")
    assert result.is_ambiguous
    assert [c.company_number for c in result.candidates] == ["01709784", "07107915"]


def test_leading_token_match_still_edges_out_a_non_leading_one():
    # All else equal, a title that starts with the query is the marginally
    # better match - the non-leading discount must not erase that.
    client = FakeClient(
        search_results=[
            {"title": "J D Wetherspoon plc", "company_number": "222", "company_status": "active", "company_type": "plc"},
            {"title": "Wetherspoon Ltd", "company_number": "111", "company_status": "active", "company_type": "ltd"},
        ]
    )
    result = resolve_company(client, "Wetherspoon")
    assert [c.company_number for c in result.candidates] == ["111", "222"]


def test_suggester_fixes_a_typo_no_transposition_can_reach():
    # "Laurant" -> "Laurent" is a substitution, which letter-shuffling can't
    # produce. The suggester is what closes that gap.
    client = FakeClient(
        search_results=[],
        search_results_by_query={
            "saint laurent": [
                {
                    "title": "YVES SAINT LAURENT UK LIMITED",
                    "company_number": "111",
                    "company_status": "active",
                    "company_type": "ltd",
                },
            ],
        },
    )
    result = resolve_company(client, "saint laurant", suggest_query=lambda _: "saint laurent")
    assert result.corrected_query == "saint laurent"
    assert result.candidates[0].company_number == "111"


def test_suggester_resolves_a_colloquial_name():
    # The real motivation: no edit-distance variant turns "spoons" into
    # "wetherspoon".
    client = FakeClient(
        search_results=[],
        search_results_by_query={
            "wetherspoon": [
                {
                    "title": "J D WETHERSPOON PLC",
                    "company_number": "01709784",
                    "company_status": "active",
                    "company_type": "plc",
                    "date_of_creation": "1983-03-25",
                },
            ],
        },
    )
    result = resolve_company(client, "spoons", suggest_query=lambda _: "wetherspoon")
    assert result.corrected_query == "wetherspoon"
    assert result.candidates[0].company_number == "01709784"


def test_cheap_transposition_wins_over_the_suggester():
    # The suggester races every search now, but a transposition that already
    # found a confident match must still be the correction used - and the
    # weak-match path never offers a separate alternative block.
    client = FakeClient(
        search_results=[],
        search_results_by_query={
            "revolut": [
                {"title": "Revolut Ltd", "company_number": "111", "company_status": "active", "company_type": "ltd"},
            ],
            "something else": [
                {"title": "Something Else Ltd", "company_number": "222", "company_status": "active", "company_type": "ltd"},
            ],
        },
    )
    result = resolve_company(client, "reovlut", suggest_query=lambda _: "something else")
    assert result.corrected_query == "revolut"
    assert result.suggestion is None
    assert [c.company_number for c in result.candidates] == ["111"]


def test_confident_match_still_offers_an_alternative():
    # "M&S" matches small M&S-prefixed firms perfectly well, so no correction
    # fires - but Marks and Spencer is offered as a separate list.
    client = FakeClient(
        search_results=[
            {
                "title": "M&S ASSET MANAGEMENT LIMITED",
                "company_number": "08432196",
                "company_status": "active",
                "company_type": "ltd",
                "date_of_creation": "2013-03-07",
            },
        ],
        search_results_by_query={
            "marks and spencer": [
                {
                    "title": "MARKS AND SPENCER P.L.C.",
                    "company_number": "00214436",
                    "company_status": "active",
                    "company_type": "plc",
                    "date_of_creation": "1926-06-17",
                },
            ],
        },
    )
    result = resolve_company(client, "M&S", suggest_query=lambda _: "marks and spencer")

    # The direct results are untouched and still ranked on their own terms.
    assert result.corrected_query is None
    assert [c.company_number for c in result.candidates] == ["08432196"]
    # The alternative is a separate, labelled list.
    assert result.suggestion is not None
    assert result.suggestion.query == "marks and spencer"
    assert [c.company_number for c in result.suggestion.candidates] == ["00214436"]


def test_no_alternative_when_the_suggestion_is_the_query():
    client = FakeClient(
        search_results=[
            {"title": "Monzo Bank Limited", "company_number": "09446231", "company_status": "active", "company_type": "ltd"},
        ],
    )
    result = resolve_company(client, "monzo", suggest_query=lambda _: "Monzo")
    assert result.suggestion is None


def test_no_alternative_when_its_results_are_weak():
    # A suggestion whose own results score badly is not worth showing.
    client = FakeClient(
        search_results=[
            {"title": "Acme Ltd", "company_number": "111", "company_status": "active", "company_type": "ltd"},
        ],
        search_results_by_query={
            "something unrelated": [
                {"title": "Totally Different Holdings", "company_number": "222", "company_status": "active", "company_type": "ltd"},
            ],
        },
    )
    result = resolve_company(client, "Acme", suggest_query=lambda _: "something unrelated")
    assert result.suggestion is None


def test_alternative_excludes_companies_already_listed():
    client = FakeClient(
        search_results=[
            {"title": "Acme Trading Ltd", "company_number": "111", "company_status": "active", "company_type": "ltd"},
        ],
        search_results_by_query={
            "acme trading": [
                {"title": "Acme Trading Ltd", "company_number": "111", "company_status": "active", "company_type": "ltd"},
            ],
        },
    )
    result = resolve_company(client, "Acme Trading", suggest_query=lambda _: "acme trading")
    assert result.suggestion is None


def test_suggester_declining_leaves_a_clean_error():
    client = FakeClient(search_results=[])
    result = resolve_company(client, "Nonexistent Qqzzx", suggest_query=lambda _: None)
    assert result.error is not None
    assert result.corrected_query is None


def test_suggester_failure_does_not_break_resolution():
    # A missing key or an outage must degrade to the deterministic path.
    def exploding(text):
        raise RuntimeError("LLM unavailable")

    client = FakeClient(search_results=[])
    result = resolve_company(client, "Nonexistent Qqzzx", suggest_query=exploding)
    assert result.error is not None
    assert "No companies found" in result.error


def test_a_correction_must_be_confident_not_merely_better():
    # Junk correcting to slightly-less-bad junk is not a correction. Both the
    # original and the suggestion return weak matches, so neither should be
    # presented as "showing results for ...".
    client = FakeClient(
        search_results=[
            {"title": "Totally Unrelated Holdings", "company_number": "999", "company_status": "active", "company_type": "ltd"},
        ],
        search_results_by_query={
            "qqzzx trading": [
                {"title": "Slightly Less Unrelated Group", "company_number": "888", "company_status": "active", "company_type": "ltd"},
            ],
        },
    )
    result = resolve_company(client, "qqzzx tradng", suggest_query=lambda _: "qqzzx trading")
    assert result.corrected_query is None
    assert [c.company_number for c in result.candidates] == ["999"]


def test_variant_search_keeps_looking_past_a_weak_hit():
    # CH returns *something* for almost any garbled string, so the first
    # variant with results is not necessarily the right spelling. The search
    # must keep going and take the best variant, not the first.
    client = FakeClient(
        search_results=[],
        search_results_by_query={
            # "erovlut" is the very first variant tried, and returns a real
            # but irrelevant company.
            "erovlut": [
                {"title": "Zebra Unrelated Holdings", "company_number": "999", "company_status": "active", "company_type": "ltd"},
            ],
            # "revolut" comes later and is the spelling actually meant.
            "revolut": [
                {"title": "Revolut Ltd", "company_number": "111", "company_status": "active", "company_type": "ltd"},
            ],
        },
    )
    result = resolve_company(client, "reovlut")
    assert result.corrected_query == "revolut"
    assert result.candidates[0].company_number == "111"


def test_no_variant_retry_when_original_match_is_already_confident():
    client = FakeClient(
        profiles={"111": {"company_name": "Acme Ltd", "company_number": "111"}},
        search_results=[
            {"title": "Acme Ltd", "company_number": "111", "company_status": "active", "company_type": "ltd"},
        ],
    )
    result = resolve_company(client, "Acme Ltd")
    assert result.is_resolved
    assert client.search_queries == ["Acme Ltd"]
