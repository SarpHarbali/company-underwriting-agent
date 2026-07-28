#!/usr/bin/env python3
"""One-off loader: Companies House bulk company data -> Postgres.

Downloads the monthly "Free Company Data Product" snapshot
(http://download.companieshouse.gov.uk/en_output.html), unpivots its wide
previous-name columns into rows, and loads the result into the `companies` and
`company_name_index` tables that name resolution queries.

This is build-time work, not a scheduled refresh: the index only has to be good
enough to *find* a company, and the live API is called for that company's actual
details the moment one is chosen, so a month-old snapshot costs nothing that
matters. Re-run it by hand when you want fresher data.

    python scripts/load_companies.py                    # download + full load
    python scripts/load_companies.py --limit 300000     # smaller demo database
    python scripts/load_companies.py --zip path/to.zip  # already downloaded
    python scripts/load_companies.py --recreate         # drop and reload

Expect ~30-60 minutes and several GB for a full load; see the README for
sizing notes before pointing this at a free-tier database.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import psycopg
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.companies_house.names import normalised_forms  # noqa: E402
from src.db import INDEXES_SQL, SCHEMA_SQL  # noqa: E402

_DOWNLOAD_URL = "https://download.companieshouse.gov.uk/BasicCompanyDataAsOneFile-{month}.zip"

# The bulk file caps former names at ten slots.
_MAX_PREVIOUS_NAMES = 10

# Rows buffered before a COPY. The two tables are written on the same
# connection, so they can't stream concurrently - chunking keeps memory flat
# while still handing Postgres batches big enough for COPY to be worth it.
_CHUNK_ROWS = 50_000

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalise_status(raw: str) -> str | None:
    """Map bulk-file statuses onto the REST API's vocabulary.

    The two products spell the same status differently ("Active - Proposal to
    Strike off" vs "active-proposal-to-strike-off"). Ranking treats status as a
    signal, so it needs one vocabulary rather than two.
    """
    normalised = _NON_ALNUM.sub("-", raw.strip().lower()).strip("-")
    return normalised or None


def _parse_date(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d/%m/%Y").date()
    except ValueError:
        return None


def _latest_available_url(session: requests.Session, months_back: int = 6) -> str:
    """Find the most recent published snapshot.

    The file is published on the 1st of each month, but not always on the 1st,
    so the current month's URL can 404 for a few days - walk backwards until
    one exists rather than hardcoding a date that goes stale.
    """
    today = date.today()
    for offset in range(months_back):
        year, month = divmod(today.year * 12 + today.month - 1 - offset, 12)
        candidate = _DOWNLOAD_URL.format(month=f"{year:04d}-{month + 1:02d}-01")
        if session.head(candidate, allow_redirects=True, timeout=30).status_code == 200:
            return candidate
    raise SystemExit(
        f"No Companies House bulk file found in the last {months_back} months. "
        f"Check {_DOWNLOAD_URL.format(month='YYYY-MM-01')} by hand."
    )


def _download(session: requests.Session, url: str, destination: Path) -> Path:
    if destination.exists():
        print(f"Using cached download: {destination} ({destination.stat().st_size / 1e9:.2f} GB)")
        return destination

    print(f"Downloading {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    downloaded = 0
    next_report = 100_000_000

    with session.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length", 0))
        with partial.open("wb") as out:
            for chunk in response.iter_content(chunk_size=1 << 20):
                out.write(chunk)
                downloaded += len(chunk)
                if downloaded >= next_report:
                    suffix = f" of {total / 1e9:.2f} GB" if total else ""
                    print(f"  {downloaded / 1e9:.2f} GB{suffix}")
                    next_report += 100_000_000

    partial.rename(destination)
    print(f"Downloaded {downloaded / 1e9:.2f} GB to {destination}")
    return destination


def _open_csv(archive: Path) -> tuple[zipfile.ZipFile, Iterator[list[str]]]:
    """Stream the CSV straight out of the zip - it decompresses to several GB."""
    zip_file = zipfile.ZipFile(archive)
    names = [n for n in zip_file.namelist() if n.lower().endswith(".csv")]
    if not names:
        raise SystemExit(f"No CSV found inside {archive}")
    stream = io.TextIOWrapper(
        zip_file.open(names[0]), encoding="utf-8", errors="replace", newline=""
    )
    return zip_file, csv.reader(stream)


def _column_positions(header: list[str]) -> dict[str, int]:
    """Map the columns we need onto their positions.

    Several headers in the published file carry a stray leading space
    (" CompanyNumber", " PreviousName_1.CompanyName"), so lookups are done on
    stripped names rather than trusting the file's spelling.
    """
    positions = {name.strip(): i for i, name in enumerate(header)}
    required = [
        "CompanyName",
        "CompanyNumber",
        "CompanyStatus",
        "IncorporationDate",
        "RegAddress.PostCode",
    ]
    missing = [name for name in required if name not in positions]
    if missing:
        raise SystemExit(f"Bulk CSV is missing expected columns: {', '.join(missing)}")
    return positions


def _flush(conn: psycopg.Connection, company_rows: list, index_rows: list) -> None:
    with conn.cursor() as cur:
        with cur.copy(
            "COPY companies (id, company_number, company_name, is_previous_name, "
            "company_status, incorporation_date, postcode) FROM STDIN"
        ) as copy:
            for row in company_rows:
                copy.write_row(row)
        with cur.copy(
            "COPY company_name_index (company_row_id, name_norm, name_stem) FROM STDIN"
        ) as copy:
            for row in index_rows:
                copy.write_row(row)
    conn.commit()
    company_rows.clear()
    index_rows.clear()


def _load_rows(conn: psycopg.Connection, archive: Path, limit: int | None, active_only: bool) -> None:
    zip_file, reader = _open_csv(archive)
    try:
        positions = _column_positions(next(reader))
        name_column = positions["CompanyName"]
        number_column = positions["CompanyNumber"]
        status_column = positions["CompanyStatus"]
        incorporated_column = positions["IncorporationDate"]
        postcode_column = positions["RegAddress.PostCode"]
        previous_name_columns = [
            positions[f"PreviousName_{i}.CompanyName"]
            for i in range(1, _MAX_PREVIOUS_NAMES + 1)
            if f"PreviousName_{i}.CompanyName" in positions
        ]

        company_rows: list[tuple] = []
        index_rows: list[tuple] = []
        next_id = 1
        companies_seen = 0

        for row in reader:
            if len(row) <= postcode_column:
                continue  # truncated line; the file has a handful

            status = _normalise_status(row[status_column])
            if active_only and status != "active":
                continue

            number = row[number_column].strip()
            if not number:
                continue

            incorporated = _parse_date(row[incorporated_column])
            postcode = row[postcode_column].strip() or None

            # The current name first, then one row per populated previous-name
            # slot - this is the unpivot. Company-level fields are repeated on
            # every row (see schema.sql).
            names = [(row[name_column].strip(), False)]
            names += [
                (row[column].strip(), True)
                for column in previous_name_columns
                if column < len(row) and row[column].strip()
            ]

            for name, is_previous in names:
                if not name:
                    continue
                name_norm, name_stem = normalised_forms(name)
                if not name_norm:
                    continue  # nothing searchable survived normalisation
                company_rows.append(
                    (next_id, number, name, is_previous, status, incorporated, postcode)
                )
                index_rows.append((next_id, name_norm, name_stem))
                next_id += 1

            companies_seen += 1
            if len(company_rows) >= _CHUNK_ROWS:
                _flush(conn, company_rows, index_rows)
                print(f"  {companies_seen:,} companies / {next_id - 1:,} name rows loaded")

            if limit is not None and companies_seen >= limit:
                break

        _flush(conn, company_rows, index_rows)
        print(f"Loaded {companies_seen:,} companies as {next_id - 1:,} name rows")
    finally:
        zip_file.close()


def _prepare_schema(conn: psycopg.Connection, recreate: bool) -> None:
    with conn.cursor() as cur:
        if recreate:
            print("Dropping existing tables")
            cur.execute("DROP TABLE IF EXISTS company_name_index, companies")
        cur.execute(SCHEMA_SQL)
        cur.execute("SELECT count(*) FROM (SELECT 1 FROM companies LIMIT 1) probe")
        if cur.fetchone()[0]:
            raise SystemExit(
                "`companies` already contains data. Re-run with --recreate to replace it."
            )
    conn.commit()


def _build_indexes(conn: psycopg.Connection) -> None:
    print("Building indexes (this is the slow part - GIN trigram indexes on millions of rows)")
    with conn.cursor() as cur:
        try:
            cur.execute("SET maintenance_work_mem = '512MB'")
        except psycopg.Error:
            # Managed providers often forbid this; index creation is merely
            # slower without it, so a refusal isn't worth failing the load for.
            conn.rollback()
        cur.execute(INDEXES_SQL)
        conn.commit()
        print("Analysing")
        cur.execute("ANALYZE companies")
        cur.execute("ANALYZE company_name_index")
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                        help="Postgres connection string (defaults to $DATABASE_URL)")
    parser.add_argument("--zip", type=Path, help="Use an already-downloaded bulk zip")
    parser.add_argument("--download-dir", type=Path, default=Path("data"),
                        help="Where to cache the download (default: ./data)")
    parser.add_argument("--limit", type=int,
                        help="Load only the first N companies - for a small demo database")
    parser.add_argument("--active-only", action="store_true",
                        help="Skip companies whose status isn't 'active', roughly halving the database")
    parser.add_argument("--recreate", action="store_true",
                        help="Drop and rebuild the tables instead of refusing to load over existing data")
    parser.add_argument("--skip-indexes", action="store_true",
                        help="Load rows without building indexes (resolution will be unusably slow)")
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("Set DATABASE_URL (or pass --database-url).")

    session = requests.Session()
    if args.zip:
        archive = args.zip
        if not archive.exists():
            raise SystemExit(f"{archive} does not exist")
    else:
        url = _latest_available_url(session)
        archive = _download(session, url, args.download_dir / url.rsplit("/", 1)[-1])

    with psycopg.connect(args.database_url) as conn:
        _prepare_schema(conn, args.recreate)
        _load_rows(conn, archive, args.limit, args.active_only)
        if args.skip_indexes:
            print("Skipping index creation (--skip-indexes)")
        else:
            _build_indexes(conn)

    print("Done.")


if __name__ == "__main__":
    main()
