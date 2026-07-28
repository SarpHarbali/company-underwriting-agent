"""Recall-oriented candidate retrieval from the local company-name index."""

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
    """A matched current or former name and its company details."""

    company_number: str
    current_name: str
    matched_name: str
    is_previous_name: bool
    company_status: str | None
    incorporation_date: date | None
    postcode: str | None


class NameRepository(Protocol):
    def find_candidates(
        self, query_norm: str, query_stem: str, variants: Sequence[str]
    ) -> list[NameMatch]: ...


_DEFAULT_BRANCH_LIMIT = 150
_DEFAULT_TOTAL_LIMIT = 500

# These thresholds retained recall in full-register testing while avoiding the
# very large candidate sets produced by pg_trgm's 0.3 default.
_DEFAULT_SIMILARITY_THRESHOLD = 0.65
_DEFAULT_WORD_SIMILARITY_THRESHOLD = 0.7

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
    -- Separate norm/stem branches avoid a full scan caused by an OR across
    -- columns. Percent signs are doubled for psycopg placeholders.
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
    """Return an escaped LIKE pattern for a leading whole-word match."""
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

        try:
            return self._run_query(query_norm, query_stem, variants)
        except psycopg.OperationalError:
            _log.warning(
                "Name index query failed; retrying with a fresh connection",
                exc_info=True,
            )
            return self._run_query(query_norm, query_stem, variants)

    def _run_query(
        self, query_norm: str, query_stem: str, variants: Sequence[str]
    ) -> list[NameMatch]:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=class_row(NameMatch)) as cur:
                # Transaction-local settings do not leak through the pool.
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
