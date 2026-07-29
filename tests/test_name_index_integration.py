from __future__ import annotations

import os
from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg_pool import ConnectionPool  # noqa: E402

from src.companies_house.name_index import PostgresNameRepository  # noqa: E402
from src.companies_house.names import normalised_forms, query_variants  # noqa: E402
from src.db import INDEXES_SQL, SCHEMA_SQL  # noqa: E402

_SCHEMA = "ch_index_test"

# (company_number, current name, [former names], status, incorporation year)
_FIXTURES = [
    ("09446231", "MONZO BANK LIMITED", [], "active", 2015),
    ("17125525", "MONZO TYRES LTD", [], "active", 2024),
    ("01709784", "J D WETHERSPOON PLC", [], "active", 1983),
    ("07107915", "WETHERSPOON & PARTNER LIMITED", [], "dissolved", 2009),
    ("11111111", "REVOLUT LTD", [], "active", 2015),
    ("00214436", "MARKS AND SPENCER P.L.C.", [], "active", 1926),
    ("08432196", "M&S ASSET MANAGEMENT LIMITED", [], "active", 2013),
    ("22222222", "ACME TRADING LIMITED", ["ZENITH TRADING LIMITED"], "active", 2001),
    ("33333333", "TOTALLY UNRELATED HOLDINGS LTD", [], "active", 2018),
]


@pytest.fixture(scope="module")
def repository():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {_SCHEMA}")

    def use_test_schema(conn):
        # pg_trgm lives in public; the fixture tables live in their own schema
        # so a stray run can't touch a real index. The commit matters - the
        # pool discards any connection left mid-transaction by `configure`.
        conn.execute(f"SET search_path TO {_SCHEMA}, public")
        conn.commit()

    pool = ConnectionPool(url, min_size=1, max_size=2, configure=use_test_schema, open=True)
    try:
        with pool.connection() as conn:
            conn.execute(SCHEMA_SQL)
            row_id = 0
            for number, name, previous_names, status, year in _FIXTURES:
                filed_names = [(name, False)] + [
                    (previous_name, True) for previous_name in previous_names
                ]
                for filed_name, is_previous in filed_names:
                    row_id += 1
                    name_norm, name_stem = normalised_forms(filed_name)
                    conn.execute(
                        "INSERT INTO companies (id, company_number, company_name,"
                        " is_previous_name, company_status, incorporation_date, postcode)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            row_id,
                            number,
                            filed_name,
                            is_previous,
                            status,
                            date(year, 1, 1),
                            "N1 1AA",
                        ),
                    )
                    conn.execute(
                        "INSERT INTO company_name_index (company_row_id, name_norm, name_stem)"
                        " VALUES (%s, %s, %s)",
                        (row_id, name_norm, name_stem),
                    )
            conn.execute(INDEXES_SQL)
        yield PostgresNameRepository(pool)
    finally:
        pool.close()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


def numbers_for(repository, query):
    query_norm, query_stem = normalised_forms(query)
    matches = repository.find_candidates(
        query_norm, query_stem, query_variants(query_norm, query_stem)
    )
    return {m.company_number for m in matches}


@pytest.mark.parametrize(
    "query, expected, branch",
    [
        ("Monzo Bank Limited", "09446231", "exact"),
        ("monzo bank ltd", "09446231", "exact on the suffix-free form"),
        ("monzo", "09446231", "prefix"),
        ("wetherspoon", "01709784", "word similarity, match not at the front"),
        ("revoult", "11111111", "one-edit transposition"),
        ("revolt", "11111111", "one-edit deletion"),
        ("revolux", "11111111", "one-edit substitution"),
        ("marks & spencer", "00214436", "'&' normalised to 'and'"),
        ("MARKS AND SPENCER PLC", "00214436", "punctuation-insensitive"),
        ("zenith trading", "22222222", "former name"),
        ("wethrspoon", "01709784", "typo against a longer registered name"),
    ],
)
def test_recall(repository, query, expected, branch):
    assert expected in numbers_for(repository, query), f"missed via {branch}"


def test_a_former_name_carries_the_current_one(repository):
    query_norm, query_stem = normalised_forms("Zenith Trading Limited")
    matches = repository.find_candidates(query_norm, query_stem, [])
    former = next(m for m in matches if m.matched_name == "ZENITH TRADING LIMITED")
    assert former.is_previous_name
    assert former.current_name == "ACME TRADING LIMITED"
    assert former.incorporation_date == date(2001, 1, 1)


def test_unrelated_names_are_not_retrieved(repository):
    assert numbers_for(repository, "monzo") == {"09446231", "17125525"}


def test_empty_query_retrieves_nothing(repository):
    assert repository.find_candidates("", "", []) == []
