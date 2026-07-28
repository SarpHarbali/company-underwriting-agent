from datetime import date

from src.companies_house.name_index import NameMatch
from src.companies_house.ranking import rank_matches, score_match


def match(name, number="111", status="active", incorporated=None, previous_of=None):
    return NameMatch(
        company_number=number,
        current_name=previous_of or name,
        matched_name=name,
        is_previous_name=previous_of is not None,
        company_status=status,
        incorporation_date=date(incorporated, 1, 1) if incorporated else None,
        postcode=None,
    )


def score(query, name, **kwargs):
    return score_match(query, match(name, **kwargs)).score


def test_exact_match_scores_highest():
    assert score("Acme Ltd", "Acme Ltd") > score("Acme Ltd", "Acme Trading Ltd")


def test_case_and_punctuation_do_not_matter():
    assert score("marks & spencer plc", "MARKS AND SPENCER P.L.C.") == score(
        "MARKS AND SPENCER P.L.C.", "MARKS AND SPENCER P.L.C."
    )


def test_suffix_only_difference_is_nearly_an_exact_match():
    # Close enough to rank alongside an exact hit, but not equal to one: two
    # distinct companies can differ only in their legal suffix.
    assert score("acme ltd", "ACME LIMITED") > score("acme ltd", "Acme Holdings Limited")
    assert score("acme ltd", "ACME LIMITED") < score("acme ltd", "Acme Ltd")


def test_only_an_identical_name_counts_as_exact():
    assert score_match("Acme Ltd", match("Acme Ltd")).is_exact_match
    assert not score_match("acme ltd", match("ACME LIMITED")).is_exact_match
    assert not score_match("Acme", match("Acme Ltd")).is_exact_match


def test_typo_still_scores_as_a_strong_match():
    # The whole point of the shortlist: one slipped letter must not drop a
    # company below the unrelated names it's competing with.
    assert score("revolut", "Revolut Ltd") > score("revoult", "Revolut Ltd")
    assert score("revoult", "Revolut Ltd") > score("revoult", "Revolution Bars Ltd")


def test_a_former_name_is_ranked_below_an_identical_current_one():
    assert score("Acme Ltd", "Acme Ltd") > score("Acme Ltd", "Acme Ltd", previous_of="Zenith Ltd")


def test_status_and_age_are_nudges_not_filters():
    # A dissolved exact match still beats a weak match on an active company.
    assert score("Acme Ltd", "Acme Ltd", status="dissolved") > score(
        "Acme Ltd", "Acme Consulting Group International"
    )
    # Age only separates otherwise-equal names.
    assert score("Acme Ltd", "Acme Ltd", incorporated=1930) > score(
        "Acme Ltd", "Acme Ltd", incorporated=2020
    )


def test_a_partial_name_is_not_penalised_by_the_length_of_the_full_one():
    # Two companies whose names both start with the query are equally good
    # name matches. Neither may pull ahead by more than the status and age
    # nudges can overturn, or results end up ordered by name length rather
    # than by which company the user is likelier to have meant.
    assert abs(score("monzo", "MONZO BANK LIMITED") - score("monzo", "MONZO TYRES LTD")) < 0.05


def test_unrelated_names_score_far_below_real_matches():
    # The gap between these two is what the weak-match threshold sits in.
    assert score("qqzzx tradng", "Totally Unrelated Holdings") < 0.5
    assert score("monzo", "MONZO BANK LIMITED") > 0.8


def test_rank_matches_keeps_the_best_row_per_company():
    ranked = rank_matches(
        "Acme Ltd",
        [
            match("Acme Holdings Ltd", "111"),
            match("Acme Ltd", "111"),
            match("Zebra Ltd", "222"),
        ],
    )
    assert [c.company_number for c in ranked] == ["111", "222"]
    assert ranked[0].matched_name == "Acme Ltd"
