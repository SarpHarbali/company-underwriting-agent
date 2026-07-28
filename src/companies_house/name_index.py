"""Recall-oriented candidate generation against the local Companies House index.

This is the retrieval half of name resolution. It answers "which few hundred
companies could this string plausibly be?" and nothing more - ordering is left
entirely to `ranking.py`, running in Python over the shortlist.

The split matters. Companies House's own search endpoint is not fuzzy, so a
single mistyped letter can return nothing at all, and the previous
implementation had to paper over that by firing a fresh API call per typo
variant. Here every variant is one more branch of one SQL statement against an
index of the full register, which makes it affordable to cast a deliberately
wide net.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol, Sequence

import psycopg
from psycopg.rows import class_row
from psycopg_pool import ConnectionPool

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NameMatch:
    """One name row from the index, plus the company it belongs to.

    `matched_name` is the name that actually matched, which may be a former
    name; `current_name` is always what the company is called today. Keeping
    both is what lets the UI say "matched on a previous name" instead of
    silently showing a name the user didn't type.
    """

    company_number: str
    current_name: str
    matched_name: str
    is_previous_name: bool
    company_status: str | None
    incorporation_date: date | None
    postcode: str | None


class NameRepository(Protocol):
    """Candidate generation, as the resolver sees it.

    A protocol so `resolve_company` never imports psycopg and tests can hand it
    a list of rows.
    """

    def find_candidates(
        self, query_norm: str, query_stem: str, variants: Sequence[str]
    ) -> list[NameMatch]: ...


# Per-branch and overall shortlist sizes. The brief asks for roughly 100-500
# candidates: wide enough that the intended company is very unlikely to have
# been excluded before Python ever sees it, small enough that reranking every
# one of them stays sub-millisecond work.
_DEFAULT_BRANCH_LIMIT = 150
_DEFAULT_TOTAL_LIMIT = 500

# Trigram thresholds. The whole-string one governs the `%` branches, and 0.65
# is well above pg_trgm's 0.3 default for a reason found the hard way: at
# full-register scale (~6.4M name rows), a query that shares its trigrams with
# a very common fragment of company names - a legal suffix ("monzo bank
# limited"), or an expanded "&" turning a short query into "m and s", one of
# the commonest word pairs in the register - passes a 0.3 threshold against a
# huge fraction of the table. "monzo bank limited" matched 294,209 of 6.4M rows
# at 0.3, a multi-second query dominated by fetching that many heap tuples to
# score, not by the sort; "m and s" stayed expensive even at 0.55. Raised in
# steps against the loaded register, re-checking recall at each one against a
# battery including deliberately adversarial cases ("aliica bank limited",
# "monzo bank limted", "marks and spencer plcc", "M&S") - recall was unchanged
# across the whole battery from 0.3 through 0.7; the extra rows admitted at
# lower thresholds were noise the reranker discards anyway, not recall the
# system depends on. 0.65 was chosen as the point past which further increases
# stopped buying meaningful speed. The word-similarity threshold governs the
# separate `<%` branch, which asks whether the query matches some run of words
# inside a name, and is kept strict because at a low value nearly every
# sufficiently long name qualifies.
_DEFAULT_SIMILARITY_THRESHOLD = 0.65
_DEFAULT_WORD_SIMILARITY_THRESHOLD = 0.7

# Each parenthesised branch is one of the candidate sets in the brief. They run
# against `company_name_index`, which holds current *and* previous names, so
# "match both current and previous names" is a property of the data rather than
# extra branches. UNION deduplicates rows; `company_number` deduplication
# happens in the reranker, which knows which of a company's names scored best.
_CANDIDATE_SQL = """
WITH matched AS (
    (SELECT company_row_id FROM company_name_index
      WHERE name_norm = %(query_norm)s OR name_stem = %(query_stem)s
      LIMIT %(branch_limit)s)
    UNION
    (SELECT company_row_id FROM company_name_index
      WHERE name_norm = ANY(%(variants)s) OR name_stem = ANY(%(variants)s)
      LIMIT %(branch_limit)s)
    UNION
    (SELECT company_row_id FROM company_name_index
      WHERE name_norm LIKE %(norm_prefix)s OR name_stem LIKE %(stem_prefix)s
      LIMIT %(branch_limit)s)
    UNION
    (SELECT company_row_id FROM company_name_index
      WHERE name_norm %% %(query_norm)s
      ORDER BY similarity(name_norm, %(query_norm)s) DESC
      LIMIT %(branch_limit)s)
    UNION
    (SELECT company_row_id FROM company_name_index
      WHERE name_stem %% %(query_stem)s
      ORDER BY similarity(name_stem, %(query_stem)s) DESC
      LIMIT %(branch_limit)s)
    UNION
    -- Word similarity: does the query match some run of words *inside* the
    -- name? This is what finds "J D WETHERSPOON PLC" from "wetherspoon",
    -- which whole-string similarity rates poorly and the prefix branch misses
    -- because the match isn't at the front. Split into two UNION members
    -- (norm, stem) rather than one `OR`-across-columns subquery: found by
    -- EXPLAIN on the loaded register that the combined form is a query-plan
    -- trap - for some literal values the planner costs it as a full sequential
    -- scan of the 6.4M-row table (9+ seconds) instead of the two bitmap index
    -- scans it uses everywhere else in this statement. Every other branch here
    -- already keeps norm and stem in separate UNION members for the same
    -- reason; this was the one exception. (Percent signs are doubled
    -- throughout this statement to escape psycopg's placeholder syntax; every
    -- doubled pair is one trigram operator.)
    (SELECT company_row_id FROM company_name_index
      WHERE %(query_norm)s <%% name_norm
      ORDER BY word_similarity(%(query_norm)s, name_norm) DESC
      LIMIT %(branch_limit)s)
    UNION
    (SELECT company_row_id FROM company_name_index
      WHERE %(query_stem)s <%% name_stem
      ORDER BY word_similarity(%(query_stem)s, name_stem) DESC
      LIMIT %(branch_limit)s)
)
SELECT
    c.company_number                              AS company_number,
    COALESCE(current.company_name, c.company_name) AS current_name,
    c.company_name                                AS matched_name,
    c.is_previous_name                            AS is_previous_name,
    c.company_status                              AS company_status,
    c.incorporation_date                          AS incorporation_date,
    c.postcode                                    AS postcode
FROM matched m
JOIN companies c ON c.id = m.company_row_id
LEFT JOIN LATERAL (
    SELECT cur.company_name
    FROM companies cur
    WHERE cur.company_number = c.company_number AND NOT cur.is_previous_name
    LIMIT 1
) current ON TRUE
LIMIT %(total_limit)s
"""


def _like_prefix(text: str) -> str:
    """A LIKE pattern matching `text` followed by a word boundary.

    Catches "monzo" -> "monzo bank limited", which whole-string trigram
    similarity rates poorly because the stored name is four times longer than
    the query.
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped} %"


class PostgresNameRepository:
    """`NameRepository` backed by the loaded bulk-data tables."""

    def __init__(
        self,
        pool: ConnectionPool,
        branch_limit: int = _DEFAULT_BRANCH_LIMIT,
        total_limit: int = _DEFAULT_TOTAL_LIMIT,
        similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
        word_similarity_threshold: float = _DEFAULT_WORD_SIMILARITY_THRESHOLD,
    ):
        self._pool = pool
        self._branch_limit = branch_limit
        self._total_limit = total_limit
        self._similarity_threshold = similarity_threshold
        self._word_similarity_threshold = word_similarity_threshold

    def find_candidates(
        self, query_norm: str, query_stem: str, variants: Sequence[str]
    ) -> list[NameMatch]:
        if not query_norm:
            return []

        # A pooled connection can die between checkout and use without the
        # pool knowing yet - the common case being a serverless provider
        # (Neon) suspending its compute and killing idle connections out from
        # under it. That failure is what `pool.connection()` discovers and
        # discards the connection for on this call's way out (logged by
        # psycopg_pool itself); without a retry here, the request that hit the
        # now-dead connection would surface that one-time infrastructure blip
        # as a user-facing error instead of transparently picking up the
        # replacement connection the pool opens in its place.
        attempts = 2
        for attempt in range(1, attempts + 1):
            try:
                return self._run_query(query_norm, query_stem, variants)
            except psycopg.OperationalError:
                if attempt == attempts:
                    raise
                _log.warning(
                    "Name index query failed on attempt %d/%d, retrying with a fresh connection",
                    attempt,
                    attempts,
                    exc_info=True,
                )
        raise AssertionError("unreachable")  # loop always returns or raises

    def _run_query(
        self, query_norm: str, query_stem: str, variants: Sequence[str]
    ) -> list[NameMatch]:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=class_row(NameMatch)) as cur:
                # Transaction-local so a pooled connection never leaks a
                # threshold into the next caller. set_config is used rather
                # than SET LOCAL because SET takes no parameters.
                cur.execute(
                    "SELECT set_config('pg_trgm.similarity_threshold', %s, true),"
                    "       set_config('pg_trgm.word_similarity_threshold', %s, true)",
                    (str(self._similarity_threshold), str(self._word_similarity_threshold)),
                )
                cur.execute(
                    _CANDIDATE_SQL,
                    {
                        "query_norm": query_norm,
                        "query_stem": query_stem or query_norm,
                        "variants": list(variants),
                        "norm_prefix": _like_prefix(query_norm),
                        "stem_prefix": _like_prefix(query_stem or query_norm),
                        "branch_limit": self._branch_limit,
                        "total_limit": self._total_limit,
                    },
                )
                return cur.fetchall()
