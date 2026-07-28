"""Postgres connection handling and DDL for the local Companies House index."""

from src.db.pool import SCHEMA_SQL, INDEXES_SQL, create_pool

__all__ = ["SCHEMA_SQL", "INDEXES_SQL", "create_pool"]
