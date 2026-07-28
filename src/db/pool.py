"""Connection pooling for the local Companies House index."""

from __future__ import annotations

from pathlib import Path

from psycopg_pool import ConnectionPool

SCHEMA_SQL = (Path(__file__).with_name("schema.sql")).read_text()
INDEXES_SQL = (Path(__file__).with_name("indexes.sql")).read_text()


def create_pool(database_url: str, min_size: int = 1, max_size: int = 4) -> ConnectionPool:
    """A small pool sized for a single-user Streamlit app.

    Opened lazily (`open=False` then `open()`) with a connect timeout rather
    than eagerly at import, so a misconfigured or unreachable database surfaces
    as a clear error at first use instead of a hang on startup. Managed
    providers idle-close connections, hence the recycle interval.
    """
    pool = ConnectionPool(
        conninfo=database_url,
        min_size=min_size,
        max_size=max_size,
        timeout=15.0,
        max_idle=300.0,
        open=False,
    )
    pool.open()
    return pool
