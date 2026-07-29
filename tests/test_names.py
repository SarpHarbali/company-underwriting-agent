import pytest

from src.companies_house.names import (
    MAX_VARIANT_QUERY_LENGTH,
    normalise_name,
    normalised_forms,
    one_edit_variants,
    query_variants,
    strip_legal_suffix,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("MONZO BANK LIMITED", "monzo bank limited"),
        ("MARKS AND SPENCER P.L.C.", "marks and spencer plc"),
        ("Marks & Spencer", "marks and spencer"),  # the two spellings must converge
        ("O'Neill's Bar Ltd", "oneills bar ltd"),
        ("  Acme   Trading  ", "acme trading"),
        ("Café Nero Ltd", "cafe nero ltd"),
        ("A.B.C. (Holdings) Ltd", "abc holdings ltd"),
        ("!!!", ""),
    ],
)
def test_normalise_name(raw, expected):
    assert normalise_name(raw) == expected


@pytest.mark.parametrize(
    "normalised, expected",
    [
        ("acme trading limited", "acme trading"),
        ("acme trading co ltd", "acme trading"),
        ("j d wetherspoon plc", "j d wetherspoon"),
        ("acme public limited company", "acme"),
        ("acme limited liability partnership", "acme"),
        ("acme", "acme"),
        ("limited", "limited"),
        ("company limited", "company"),
    ],
)
def test_strip_legal_suffix(normalised, expected):
    assert strip_legal_suffix(normalised) == expected


def test_one_edit_variants_cover_the_four_slip_types():
    variants = set(one_edit_variants("monzo"))
    assert "mnozo" in variants  # transposition
    assert "monz" in variants  # deletion
    assert "monxo" in variants  # substitution
    assert "monzoo" in variants  # insertion
    assert "monzo" not in variants  # the input itself is not a variant


def test_variants_are_already_normalised():
    for variant in one_edit_variants("john lewis"):
        assert variant == normalise_name(variant)


def test_no_variants_for_long_queries():
    assert one_edit_variants("a" * (MAX_VARIANT_QUERY_LENGTH + 1)) == []


def test_query_variants_cover_both_forms():
    variants = set(query_variants(*normalised_forms("acme ltd")))
    assert "acme ldt" in variants  # a slip in the full form
    assert "acne" in variants  # ... and one in the suffix-free form
