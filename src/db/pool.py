from __future__ import annotations

from pathlib import Path

from psycopg_pool import ConnectionPool

SCHEMA_SQL = (Path(__file__).with_name("schema.sql")).read_text()
INDEXES_SQL = (Path(__file__).with_name("indexes.sql")).read_text()


def create_pool(database_url: str, min_size: int = 1, max_size: int = 4) -> ConnectionPool:
    pool = ConnectionPool(
        conninfo=database_url,
        min_size=min_size,
        max_size=max_size,
        timeout=15.0,
        max_idle=300.0,
        # The pool outlives long idle spells (cached for the process lifetime,
        # while a managed Postgres autosuspends its compute), so connections
        # can be dead by the time they're handed out. Checking on checkout
        # trades a round-trip for not failing the first query after a lull.
        check=ConnectionPool.check_connection,
        open=False,
    )
    pool.open()
    return pool
